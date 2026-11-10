"""Three example programs, each in a different IEC 61131-3 style, plus the process they control.

motor          ladder rungs (LD) written in the text rung language
tank           Structured Text style (ST): level control with hysteresis and interlocks
traffic_light  a step sequence (SFC style): states, transitions and timers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from plcsim.blocks import RS, TON, R_TRIG
from plcsim.ladder import Compiled, Env, compile_program
from plcsim.runtime import PLC, Tag


class LadderProgram:
    """Runs rungs written in plcsim.ladder against the PLC's tags."""

    def __init__(self, name: str, source: str, tags: list[Tag], description: str = "") -> None:
        self.name, self.source, self.tags, self.description = name, source, tags, description
        self.compiled: Compiled = compile_program(source, {t.name: t.area for t in tags})
        self.blocks: dict[str, Any] = {}

    def setup(self, plc: PLC) -> None:
        self.blocks = self.compiled.new_blocks()

    def execute(self, plc: PLC, dt: float) -> None:
        env = Env(read=plc.read, blocks=self.blocks)
        for rung in self.compiled.rungs:
            rung.run(env, plc.write, dt)

    def check_setpoints(self, setpoints: dict[str, float]) -> str | None:
        return None


# ---------------------------------------------------------------- motor (LD)
MOTOR_TAGS = [
    Tag("START", "I", description="Start push button (normally open)"),
    Tag("STOP_OK", "I", default=True, description="Stop push button, normally closed: TRUE while NOT pressed"),
    Tag("OVERLOAD", "I", description="Thermal overload relay tripped"),
    Tag("RUNNING", "I", description="Contactor auxiliary contact: the motor really is running"),
    Tag("RESET_PB", "I", description="Fault reset push button"),
    Tag("MOTOR", "Q", description="Motor contactor coil"),
    Tag("LAMP_RUN", "Q", description="Green lamp"),
    Tag("LAMP_FAULT", "Q", description="Red lamp"),
    Tag("FAULT", "M", description="Latched fault: overload, or contactor did not pull in"),
]

MOTOR_RUNGS = """\
# Rung 1: a latched fault stops everything until someone presses RESET_PB
SET FAULT IF OVERLOAD OR T_NO_FEEDBACK.Q
RESET FAULT IF RESET_PB AND NOT OVERLOAD

# Rung 2: classic start/stop with seal-in. STOP_OK is wired normally closed,
# so a broken wire stops the motor instead of making it impossible to stop.
MOTOR := (START OR MOTOR) AND STOP_OK AND NOT FAULT

# Rung 3: the contactor must confirm within 2 s, or we latch a fault
TON T_NO_FEEDBACK(IN := MOTOR AND NOT RUNNING, PT := T#2s)

# Rung 4: lamps
LAMP_RUN := MOTOR AND RUNNING
LAMP_FAULT := FAULT
"""


def motor_program(source: str = MOTOR_RUNGS) -> LadderProgram:
    return LadderProgram("motor", source, MOTOR_TAGS, "Motor start/stop with seal-in, overload and contactor feedback")


@dataclass
class MotorProcess:
    """A motor with a contactor. Set contactor_broken to simulate a coil that no longer pulls in."""

    running: bool = False
    contactor_broken: bool = False

    def sense(self) -> dict[str, Any]:
        return {"RUNNING": self.running}

    def step(self, outputs: dict[str, Any], dt: float) -> None:
        self.running = bool(outputs["MOTOR"]) and not self.contactor_broken


# ---------------------------------------------------------------- tank (ST)
TANK_TAGS = [
    Tag("LEVEL", "I", float, 50.0, "%", -25, 125, "Level transmitter, 4-20 mA scaled to 0-100 % (out of range = sensor fault)"),
    Tag("LSHH", "I", description="High-high level switch, independent of the transmitter"),
    Tag("ESTOP_OK", "I", default=True, description="Emergency stop chain healthy (normally closed)"),
    Tag("PUMP_RUNNING", "I", description="Pump motor feedback"),
    Tag("PUMP", "Q", description="Fill pump"),
    Tag("BEACON", "Q", description="Alarm beacon"),
    Tag("AUTO", "M", default=True, description="Automatic level control enabled"),
    Tag("PUMP_FAULT", "M", description="Pump commanded but no feedback for 3 s"),
    Tag("SP_LOW", "SP", float, 30.0, "%", 5, 80, "Start the pump below this level"),
    Tag("SP_HIGH", "SP", float, 70.0, "%", 20, 90, "Stop the pump above this level"),
    Tag("START_AUTO", "CMD", description="Enable automatic control"),
    Tag("STOP_AUTO", "CMD", description="Disable automatic control (pump stops)"),
    Tag("RESET_FAULT", "CMD", description="Reset a latched pump fault"),
]


class TankProgram:
    """Level control written the way Structured Text reads: plain statements, top to bottom.

    The equivalent ST is in docs/PLC-SIMULADOR.md. The interlocks come last on purpose:
    whatever the control logic decided above, they have the final word on the pump.
    """

    name = "tank"
    description = "Tank level control with hysteresis, pump supervision and safety interlocks"
    tags = TANK_TAGS

    def setup(self, plc: PLC) -> None:
        self.fault = RS()
        self.no_feedback = TON(3.0)
        self.low_level = TON(5.0)
        self.start_edge = R_TRIG()
        self.filling = False

    def execute(self, plc: PLC, dt: float) -> None:
        I, Q, M, SP, CMD = plc.I, plc.Q, plc.M, plc.SP, plc.CMD
        # operator commands
        if self.start_edge(CMD["START_AUTO"]):
            M["AUTO"] = True
        if CMD["STOP_AUTO"]:
            M["AUTO"] = False
        # hysteresis: start below SP_LOW, stop above SP_HIGH, keep the decision in between
        if I["LEVEL"] <= SP["SP_LOW"]:
            self.filling = True
        elif I["LEVEL"] >= SP["SP_HIGH"]:
            self.filling = False
        # pump supervision: commanded but not running for 3 s latches a fault
        M["PUMP_FAULT"] = self.fault(S=self.no_feedback(Q["PUMP"] and not I["PUMP_RUNNING"], dt), R1=CMD["RESET_FAULT"])
        pump = M["AUTO"] and self.filling and not M["PUMP_FAULT"]
        # transmitter health (NAMUR NE43): below 3.6 mA (-2.5 %) or above 21 mA (106 %) is a
        # failed sensor, not a level. Fail safe: stop filling when the level is unknown.
        sensor_bad = I["LEVEL"] < -2.5 or I["LEVEL"] > 106.0
        # interlocks: hard-wired logic the control part cannot override
        interlocked = not I["ESTOP_OK"] or I["LSHH"] or I["LEVEL"] >= 95.0 or sensor_bad
        Q["PUMP"] = pump and not interlocked
        # alarms
        plc.alarm("LEVEL_HIGH_HIGH", I["LSHH"] or (I["LEVEL"] >= 95.0 and not sensor_bad), "Level high-high: pump interlocked", "critical")
        plc.alarm("LEVEL_LOW", self.low_level(I["LEVEL"] < 10.0 and not sensor_bad, dt), "Level below 10 % for 5 s", "high")
        plc.alarm("LEVEL_SENSOR_FAULT", sensor_bad, "Level transmitter signal out of range (wire break?): pump stopped", "critical")
        plc.alarm("PUMP_FAULT", M["PUMP_FAULT"], "Pump commanded but not running", "high")
        plc.alarm("ESTOP", not I["ESTOP_OK"], "Emergency stop pressed", "critical")
        Q["BEACON"] = any(a.active and not a.acknowledged for a in plc.alarms.values())

    def check_setpoints(self, setpoints: dict[str, float]) -> str | None:
        if setpoints["SP_HIGH"] - setpoints["SP_LOW"] < 10:
            return "SP_HIGH must be at least 10 % above SP_LOW, or the pump would start and stop constantly"
        return None


@dataclass
class TankProcess:
    """A tank filled by a pump and emptied by a consumer.

    Level changes by (inflow - outflow) every second. Set pump_broken to simulate a
    pump that does not start, or demand to change how fast the plant consumes water.
    """

    level: float = 50.0
    pump_flow: float = 2.0
    demand: float = 0.8
    pump_broken: bool = False
    running: bool = False

    def sense(self) -> dict[str, Any]:
        return {"LEVEL": round(self.level, 3), "LSHH": self.level >= 97.0, "PUMP_RUNNING": self.running}

    def step(self, outputs: dict[str, Any], dt: float) -> None:
        self.running = bool(outputs["PUMP"]) and not self.pump_broken
        inflow = self.pump_flow if self.running else 0.0
        self.level = min(100.0, max(0.0, self.level + (inflow - self.demand) * dt))


# ---------------------------------------------------------------- traffic light (SFC)
TRAFFIC_TAGS = [
    Tag("PED_BUTTON", "I", description="Pedestrian request button"),
    Tag("CAR_RED", "Q"), Tag("CAR_AMBER", "Q"), Tag("CAR_GREEN", "Q"),
    Tag("WALK", "Q", description="Pedestrian green"),
    Tag("REQUEST", "M", description="A pedestrian is waiting"),
    Tag("STEP", "M", float, 0.0, description="Active step number of the sequence"),
]


@dataclass
class _Step:
    name: str
    outputs: tuple[str, ...]
    minimum: float


class TrafficLightProgram:
    """A sequence of steps. Each step sets outputs; a transition moves to the next step.

    This is how Sequential Function Charts (SFC) work: exactly one step is active,
    and each transition has a condition (here: time in step, plus the pedestrian request).
    """

    name = "traffic_light"
    description = "Pedestrian crossing: cars stay green until someone presses the button"
    tags = TRAFFIC_TAGS
    steps = [
        _Step("CARS_GO", ("CAR_GREEN",), 10.0),
        _Step("CARS_AMBER", ("CAR_AMBER",), 3.0),
        _Step("ALL_RED", ("CAR_RED",), 1.0),
        _Step("WALK", ("CAR_RED", "WALK"), 8.0),
        _Step("CLEARANCE", ("CAR_RED",), 2.0),
    ]

    def setup(self, plc: PLC) -> None:
        self.step = 0
        self.timer = TON()
        self.press = R_TRIG()

    def execute(self, plc: PLC, dt: float) -> None:
        if self.press(plc.I["PED_BUTTON"]):
            plc.M["REQUEST"] = True
        current = self.steps[self.step]
        elapsed = self.timer(True, dt, current.minimum)
        ready = elapsed and (current.name != "CARS_GO" or plc.M["REQUEST"])
        if ready:
            self.step = (self.step + 1) % len(self.steps)
            self.timer(False, dt)
            if self.steps[self.step].name == "WALK":
                plc.M["REQUEST"] = False
        active = self.steps[self.step]
        for name in ("CAR_RED", "CAR_AMBER", "CAR_GREEN", "WALK"):
            plc.Q[name] = name in active.outputs
        plc.M["STEP"] = float(self.step)

    def check_setpoints(self, setpoints: dict[str, float]) -> str | None:
        return None


@dataclass
class Scenario:
    name: str
    plc: PLC
    actors: dict[str, Any] = field(default_factory=dict)


def build(name: str) -> Scenario:
    """A ready-to-run PLC with its process model."""
    if name == "motor":
        process = MotorProcess()
        return Scenario(name, PLC(motor_program(), process), {"process": process})
    if name == "tank":
        process = TankProcess()
        return Scenario(name, PLC(TankProgram(), process), {"process": process})
    if name == "traffic_light":
        return Scenario(name, PLC(TrafficLightProgram()))
    raise ValueError(f"unknown program {name!r}; choose motor, tank or traffic_light")


PROGRAMS = ("motor", "tank", "traffic_light")
