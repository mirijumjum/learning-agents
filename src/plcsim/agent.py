"""Two agents that work next to a PLC, never inside it.

OperatorAgent   answers questions about the plant and makes the few changes an operator
                may make: setpoints within limits, HMI buttons, acknowledging alarms.
                It can also run a what-if on a copy of the plant (a digital twin) before
                suggesting a change. It cannot touch outputs, inputs, forces or the mode,
                and the PLC still validates everything it writes.

LadderCoder     writes rungs from a description. The rungs are compiled and then tested
                against the simulator; only code that passes every test is returned.
                That is the pattern for any LLM-generated control logic: the model
                proposes, deterministic tests decide, a person approves.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable

from agent_framework import Agent, BaseChatClient, ChatResponse, Content, FunctionInvocationLayer, Message, tool
from pydantic import Field

from plcsim.ladder import LadderError
from plcsim.programs import LadderProgram, MotorProcess, Scenario, MOTOR_TAGS
from plcsim.runtime import PLC, Tag, WriteRejected

_THINK = re.compile(r"<think>.*?</think>", re.S)

OPERATOR_INSTRUCTIONS = """\
You are an operator assistant for a PLC-controlled plant. Answer in the user's language.
Facts come only from your tools: call read_plant before describing the plant, and never
state a value you did not read. Keep answers short and concrete, with units.
You may change setpoints, press HMI buttons and acknowledge alarms, and only when the
user asks for it. Before a change that could raise an alarm, call predict to check it on
a copy of the plant and say what you expect. You cannot override interlocks, emergency
stops, outputs or forces: the PLC decides those. If a write is refused, say why in plain words.
Never acknowledge alarms the user did not ask you to acknowledge."""

CODER_INSTRUCTIONS = """\
You write PLC logic in a small rung language close to IEC 61131-3 Structured Text.
Reply with the complete program in one ```ladder block and nothing else.

Grammar (one rung per line, # starts a comment):
  TAG := expression                     assign a Q or M tag every scan
  SET TAG IF expression                 latch TRUE when the condition is TRUE
  RESET TAG IF expression               unlatch when the condition is TRUE
  TON NAME(IN := expression, PT := T#2s)   on-delay timer; read NAME.Q and NAME.ET
  TOF / TP same inputs; CTU NAME(CU := e, R := e, PV := 3) read NAME.Q, NAME.CV
  R_TRIG NAME(CLK := e) gives NAME.Q for one scan on a rising edge
Operators: NOT, AND, OR, XOR, parentheses, comparisons < > <= >= = <>, + - * /.
Rules: write only Q and M tags, never I tags. Use only the tag names listed, spelled exactly.
Normally-closed inputs (names ending in _OK) are TRUE when healthy and FALSE when pressed or tripped.
A self-holding output reads itself: OUT := (ON_BUTTON OR OUT) AND OFF_OK.
Prefer one assignment per output; use SET/RESET only for latched faults.

Example, for a different machine (a conveyor that runs 10 s after a box is detected):
```ladder
# Jam fault latches until the reset button
SET JAM IF T_JAM.Q
RESET JAM IF RESET_BTN
# Run while a box is present, and for 10 s after it leaves, unless jammed
TOF T_RUN(IN := BOX_SENSOR, PT := T#10s)
BELT := T_RUN.Q AND GUARD_OK AND NOT JAM
TON T_JAM(IN := BELT AND NOT BELT_MOVING, PT := T#3s)
```"""


# ------------------------------------------------------------------ model clients
def make_client(role: str, profile: str | None = None) -> Any:
    profile = profile or os.environ.get("PLCSIM_PROFILE", "ollama")
    if profile == "mock":
        return MockPLCClient(role)
    if profile != "ollama":
        raise ValueError("PLCSIM_PROFILE must be ollama or mock")
    from agent_framework.openai import OpenAIChatCompletionClient

    model = os.environ.get("PLCSIM_MODEL", "qwen3:8b")
    return OpenAIChatCompletionClient(model=model, api_key="ollama", base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"))


def _options(profile: str | None) -> dict[str, Any]:
    options: dict[str, Any] = {"max_tokens": 2000}
    if (profile or os.environ.get("PLCSIM_PROFILE", "ollama")) == "ollama":
        options["reasoning_effort"] = "none"  # qwen3 would otherwise spend the budget thinking
    return options


async def _run(agent: Agent, prompt: str, profile: str | None, timeout: float, effort: str | None = None) -> str:
    options = _options(profile)
    if effort and "reasoning_effort" in options:
        options.update(reasoning_effort=effort, max_tokens=8000)
    try:
        response = await asyncio.wait_for(agent.run(prompt, options=options), timeout=timeout)
    except asyncio.TimeoutError:
        return f"The model took longer than {timeout:.0f}s, so I stopped it."
    return _THINK.sub("", response.text or "").strip()


# ------------------------------------------------------------------ operator agent
def predict(scenario: Scenario, seconds: float, setpoints: dict[str, float] | None = None) -> dict[str, Any]:
    """Run a deep copy of the PLC and its process forward. The real plant is not touched."""
    # The copy leaves out the scan hooks: signal generators, event publishers and webhooks
    # belong to the real plant, and a what-if must not send events about a future that may never happen.
    memo = {id(scenario.plc.before_scan): [], id(scenario.plc.after_scan): []}
    twin = copy.deepcopy(scenario, memo)
    plc = twin.plc
    for name, value in (setpoints or {}).items():
        plc.write_setpoint(name, value, who="what-if")
    floats = [t.name for t in plc.program.tags if t.area == "I" and t.kind is float]
    lows = {n: plc.I.get(n, plc.field_inputs[n]) for n in floats}
    highs = dict(lows)
    starts = {name: 0 for name in plc.Q}
    first_start: dict[str, float] = {}
    previous = dict(plc.Q)
    raised: list[str] = []
    for _ in range(max(1, round(min(seconds, 3600) / plc.scan_time))):
        plc.scan()
        for n in floats:
            lows[n], highs[n] = min(lows[n], plc.I[n]), max(highs[n], plc.I[n])
        for name, value in plc.Q.items():
            if value and not previous[name]:
                starts[name] += 1
                first_start.setdefault(name, round(plc.time - scenario.plc.time, 1))
        previous = dict(plc.Q)
        raised += [a for a in plc.alarms if a not in raised]
    return {
        "simulated_seconds": round(plc.time - scenario.plc.time),
        "range": {n: {"min": round(lows[n], 1), "max": round(highs[n], 1), "end": round(plc.I[n], 1)} for n in floats},
        "output_starts": {k: v for k, v in starts.items() if v},
        "first_start_after_s": first_start,
        "alarms_raised": raised,
        "end_outputs": dict(plc.Q),
    }


@dataclass
class OperatorTools:
    scenario: Scenario
    allow_writes: bool = True
    owns_clock: bool = True  # True: the agent's actions advance simulated time; False: a live scan loop does
    write_budget: int = 3
    writes: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def plc(self) -> PLC:
        return self.scenario.plc

    def _settle(self) -> None:
        if self.owns_clock:
            self.plc.run_for(1.0)

    def _spend(self) -> str | None:
        if not self.allow_writes:
            return "REFUSED: this assistant is read-only."
        if self.writes >= self.write_budget:
            return f"REFUSED: at most {self.write_budget} changes per request. Ask again to make more."
        self.writes += 1
        return None

    def build(self) -> list[Any]:
        plc = self.plc

        def read_plant() -> str:
            """Read every tag, the active alarms, the setpoint limits and the available buttons."""
            snapshot = plc.snapshot()
            snapshot["setpoint_limits"] = {t.name: [t.low, t.high] for t in plc.program.tags if t.area == "SP"}
            snapshot["buttons"] = {t.name: t.description for t in plc.program.tags if t.area == "CMD"}
            snapshot["descriptions"] = {t.name: t.description for t in plc.program.tags if t.description}
            return json.dumps(snapshot)

        def set_setpoint(
            name: Annotated[str, Field(description="Setpoint tag, for example SP_HIGH")],
            value: Annotated[float, Field(description="New value in the setpoint's unit")],
        ) -> str:
            """Change one operator setpoint. The PLC rejects values outside its limits."""
            refused = self._spend()
            if refused:
                return refused
            try:
                old = plc.SP.get(name.upper())
                plc.write_setpoint(name.upper(), value, who="agent")
            except WriteRejected as exc:
                self.writes -= 1
                return f"REFUSED by the PLC: {exc}"
            self.log.append({"tool": "set_setpoint", "name": name.upper(), "old": old, "new": value})
            self._settle()
            return json.dumps({"changed": name.upper(), "from": old, "to": value})

        def press_button(name: Annotated[str, Field(description="HMI button (CMD tag), for example START_AUTO")]) -> str:
            """Press an HMI button once. Interlocks still apply."""
            refused = self._spend()
            if refused:
                return refused
            try:
                plc.command(name.upper(), who="agent")
            except WriteRejected as exc:
                self.writes -= 1
                return f"REFUSED by the PLC: {exc}"
            self.log.append({"tool": "press_button", "name": name.upper()})
            self._settle()
            return json.dumps({"pressed": name.upper(), "outputs_now": plc.Q, "memory_now": plc.M})

        def acknowledge_alarms() -> str:
            """Acknowledge every active alarm. Only when the user asked for it."""
            refused = self._spend()
            if refused:
                return refused
            acked = plc.acknowledge(who="agent")
            self.log.append({"tool": "acknowledge_alarms", "alarms": acked})
            self._settle()
            return json.dumps({"acknowledged": acked, "still_listed": [a.as_dict() for a in plc.alarms.values()]})

        def predict_tool(
            seconds: Annotated[float, Field(description="How far ahead to simulate, in seconds (max 3600)")],
            setpoints: Annotated[dict[str, float] | None, Field(description="Optional setpoint changes to try, e.g. {\"SP_HIGH\": 85}")] = None,
        ) -> str:
            """What-if on a copy of the plant: changes nothing, returns min/max/end values and alarms."""
            try:
                return json.dumps(predict(self.scenario, seconds, {k.upper(): v for k, v in (setpoints or {}).items()}))
            except WriteRejected as exc:
                return f"That setpoint would be refused by the PLC: {exc}"

        tools = [tool(read_plant, name="read_plant"), tool(predict_tool, name="predict")]
        if self.allow_writes:
            tools += [tool(fn, name=fn.__name__) for fn in (set_setpoint, press_button, acknowledge_alarms)]
        return tools


class OperatorAgent:
    def __init__(self, scenario: Scenario, profile: str | None = None, allow_writes: bool = True, owns_clock: bool = True, timeout: float = 180.0) -> None:
        self.scenario, self.profile, self.timeout = scenario, profile, timeout
        self.allow_writes, self.owns_clock = allow_writes, owns_clock
        self.client = make_client("operator", profile)

    async def ask(self, question: str) -> tuple[str, OperatorTools]:
        tools = OperatorTools(self.scenario, self.allow_writes, self.owns_clock)
        agent = Agent(self.client, OPERATOR_INSTRUCTIONS, name="operator", tools=tools.build())
        answer = await _run(agent, question, self.profile, self.timeout)
        return answer, tools


# ------------------------------------------------------------------ event-driven use
ALARM_QUESTION = (
    "Acaba de saltar la alarma {name} en t={t}s: \"{message}\". Lee la planta, explica la causa más probable "
    "citando los valores que leas (con unidades) y di qué debe comprobar primero el operador. No cambies nada."
)


async def explain_alarms(scenario: Scenario, alarms: list[Any], profile: str | None = None) -> list[tuple[str, str]]:
    """Ask a read-only operator agent about each raised alarm event. Returns (alarm, explanation)."""
    agent = OperatorAgent(scenario, profile=profile, allow_writes=False)
    answers = []
    for event in alarms:
        question = ALARM_QUESTION.format(name=event.payload.get("name"), t=event.t, message=event.payload.get("message", ""))
        answer, _ = await agent.ask(question)
        answers.append((event.payload.get("name"), answer))
    return answers


# ------------------------------------------------------------------ ladder coder
@dataclass
class Check:
    """Set some inputs, let the PLC run, then expect some tags to have given values."""

    description: str
    set: dict[str, Any] = field(default_factory=dict)
    run: float = 0.5
    expect: dict[str, Any] = field(default_factory=dict)


MOTOR_SPEC = (
    "Motor start/stop. START starts the motor and it keeps running after START is released. "
    "Opening STOP_OK stops it. OVERLOAD, or no RUNNING feedback within 2 s of starting, latches FAULT, "
    "which stops the motor until RESET_PB is pressed with no overload. LAMP_RUN is on while the motor "
    "runs, LAMP_FAULT while FAULT is set."
)

MOTOR_CHECKS = [
    Check("idle at power-up", run=0.5, expect={"MOTOR": False, "LAMP_FAULT": False}),
    Check("START starts it", set={"START": True}, run=0.3, expect={"MOTOR": True}),
    Check("it keeps running after START is released", set={"START": False}, run=1.0, expect={"MOTOR": True, "LAMP_RUN": True}),
    Check("STOP_OK open stops it", set={"STOP_OK": False}, run=0.3, expect={"MOTOR": False}),
    Check("it stays stopped after STOP is released", set={"STOP_OK": True}, run=0.5, expect={"MOTOR": False}),
    Check("overload latches a fault", set={"START": True, "OVERLOAD": True}, run=0.3, expect={"MOTOR": False, "FAULT": True, "LAMP_FAULT": True}),
    Check("the fault stays after the overload clears", set={"START": False, "OVERLOAD": False}, run=0.5, expect={"FAULT": True, "MOTOR": False}),
    Check("RESET_PB clears it", set={"RESET_PB": True}, run=0.3, expect={"FAULT": False}),
    Check("a broken contactor latches a fault after 2 s", set={"RESET_PB": False, "START": True, "@contactor_broken": True}, run=0.3, expect={"MOTOR": True}),
    Check("...and stops the motor", set={"START": False}, run=2.5, expect={"FAULT": True, "MOTOR": False}),
]


def run_checks(source: str, tags: list[Tag], checks: list[Check], make_process: Callable[[], Any] | None = MotorProcess) -> list[str]:
    """Compile and test rungs. Returns the failures; an empty list means every check passed."""
    try:
        program = LadderProgram("candidate", source, tags)
    except LadderError as exc:
        return [f"does not compile: {exc}"]
    process = make_process() if make_process else None
    plc = PLC(program, process)
    failures = []
    for check in checks:
        for name, value in check.set.items():
            if name.startswith("@"):
                setattr(process, name[1:], value)
            else:
                plc.set_input(name, value)
        plc.run_for(check.run)
        wrong = {name: plc.read(name) for name, want in check.expect.items() if plc.read(name) != want}
        if wrong:
            failures.append(f"{check.description}: expected {check.expect}, got {wrong}")
    return failures


@dataclass
class CoderResult:
    source: str
    passed: bool
    attempts: int
    history: list[dict[str, Any]]


class LadderCoder:
    def __init__(self, profile: str | None = None, timeout: float = 300.0, effort: str | None = "low") -> None:
        """effort: how much the model may think before answering. Writing logic is design work, so a little helps."""
        self.profile, self.timeout, self.effort = profile, timeout, effort
        self.client = make_client("coder", profile)

    async def write(self, spec: str = MOTOR_SPEC, tags: list[Tag] = MOTOR_TAGS, checks: list[Check] = MOTOR_CHECKS, attempts: int = 3) -> CoderResult:
        tag_list = "\n".join(f"  {t.name:12} {t.area:3} {t.description}" for t in tags)
        prompt = f"Tags:\n{tag_list}\n\nWrite the program for: {spec}"
        history: list[dict[str, Any]] = []
        source = ""
        for attempt in range(1, attempts + 1):
            agent = Agent(self.client, CODER_INSTRUCTIONS, name="coder")
            answer = await _run(agent, prompt, self.profile, self.timeout, effort=self.effort)
            source = _extract(answer)
            failures = run_checks(source, tags, checks) if source else ["no ```ladder block in the answer"]
            history.append({"attempt": attempt, "failures": failures, "source": source})
            if not failures:
                return CoderResult(source, True, attempt, history)
            prompt = (
                f"Tags:\n{tag_list}\n\nWrite the program for: {spec}\n\nYour previous program:\n```ladder\n{source}\n```\n"
                "It failed these checks:\n- " + "\n- ".join(failures) + "\nFix it and reply with the complete program."
            )
        return CoderResult(source, False, attempts, history)


def _extract(answer: str) -> str:
    match = re.search(r"```(?:ladder|st|iecst|text)?\s*\n(.*?)```", answer, re.S | re.I)
    return (match.group(1) if match else "").strip()


# ------------------------------------------------------------------ offline model
_MOCK_FIRST_TRY = """\
MOTOR := START AND STOP_OK AND NOT OVERLOAD
LAMP_RUN := MOTOR
"""
_MOCK_FIXED = """\
SET FAULT IF OVERLOAD OR T_FB.Q
RESET FAULT IF RESET_PB AND NOT OVERLOAD
MOTOR := (START OR MOTOR) AND STOP_OK AND NOT FAULT
TON T_FB(IN := MOTOR AND NOT RUNNING, PT := T#2s)
LAMP_RUN := MOTOR AND RUNNING
LAMP_FAULT := FAULT
"""


class MockPLCClient(FunctionInvocationLayer, BaseChatClient):
    """A scripted model for tests and for trying the CLI without Ollama. It calls real tools."""

    OTEL_PROVIDER_NAME = "mock"

    def __init__(self, role: str) -> None:
        super().__init__()
        self.role = role

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):  # type: ignore[override]
        if stream:
            raise NotImplementedError
        reply = Message(role="assistant", contents=[self._answer(messages)])
        return ChatResponse(messages=[reply], model="mock", usage_details={"input_token_count": 0, "output_token_count": 0, "total_token_count": 0})

    def _answer(self, messages: list[Message]) -> Content | str:
        prompt = "\n".join(m.text for m in messages if m.role == "user")
        if self.role == "coder":
            return f"```ladder\n{_MOCK_FIXED if 'failed these checks' in prompt else _MOCK_FIRST_TRY}```"
        results = [c for m in messages for c in m.contents if c.type == "function_result"]
        plan: list[tuple[str, dict[str, Any]]] = [("read_plant", {})]
        for name, value in re.findall(r"\b(SP_LOW|SP_HIGH)\b\D{0,12}?(\d+(?:\.\d+)?)", prompt, re.I):
            if re.search(r"what if|qué pasa|que pasa|predic|simula", prompt, re.I):
                plan.append(("predict", {"seconds": 300, "setpoints": {name.upper(): float(value)}}))
            else:
                plan.append(("set_setpoint", {"name": name.upper(), "value": float(value)}))
        if re.search(r"acknowledge|reconoce", prompt, re.I):
            plan.append(("acknowledge_alarms", {}))
        if re.search(r"\b(start|arranca|activa)\b", prompt, re.I):
            plan.append(("press_button", {"name": "START_AUTO"}))
        if re.search(r"\bE-?STOP\b|override|puentea|anula", prompt, re.I):
            plan.append(("press_button", {"name": "ESTOP_OK"}))
        if len(results) < len(plan):
            name, arguments = plan[len(results)]
            return Content.from_function_call(call_id=f"call_{len(results)}", name=name, arguments=arguments)
        return "Tool results:\n" + "\n".join(str(r.result) for r in results)
