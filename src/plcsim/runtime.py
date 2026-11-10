"""The PLC itself: tags, the scan cycle, alarms, forcing and a write audit.

A real PLC repeats one loop forever, every few milliseconds:

    1. read inputs   copy every sensor into the input image (I)
    2. execute       run the program once, top to bottom, using only that image
    3. write outputs copy the output image (Q) to the actuators

Because the program reads a frozen copy of the inputs, a sensor that changes in
the middle of a scan cannot give two rungs two different answers. This module
reproduces that loop, and runs it against a process model instead of real wiring.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

Area = Literal["I", "Q", "M", "SP", "CMD"]


@dataclass(frozen=True)
class Tag:
    """One named variable.

    I    input from the field (sensor, button). Read-only for the program.
    Q    output to the field (motor, valve, lamp). Written only by the program.
    M    internal memory (state, latched faults).
    SP   setpoint the operator may change, always within [low, high].
    CMD  momentary operator command (an HMI push button): TRUE for one scan.
    """

    name: str
    area: Area
    kind: type = bool
    default: Any = None
    unit: str = ""
    low: float | None = None
    high: float | None = None
    description: str = ""

    @property
    def initial(self) -> Any:
        if self.default is not None:
            return self.default
        return False if self.kind is bool else 0.0


class Program(Protocol):
    name: str
    description: str
    tags: list[Tag]

    def setup(self, plc: "PLC") -> None: ...
    def execute(self, plc: "PLC", dt: float) -> None: ...
    def check_setpoints(self, setpoints: dict[str, float]) -> str | None: ...


class Process(Protocol):
    """A model of the machine the PLC controls: it turns outputs into sensor readings."""

    def sense(self) -> dict[str, Any]: ...
    def step(self, outputs: dict[str, Any], dt: float) -> None: ...


class WriteRejected(ValueError):
    """A write that the PLC refused. The message is safe to show an operator."""


@dataclass
class Alarm:
    name: str
    message: str
    severity: str
    active: bool = True
    acknowledged: bool = False
    since: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "message": self.message, "severity": self.severity,
                "active": self.active, "acknowledged": self.acknowledged, "since_s": round(self.since, 1)}


@dataclass
class PLC:
    program: Program
    process: Process | None = None
    scan_time: float = 0.1
    mode: Literal["RUN", "STOP"] = "RUN"
    time: float = 0.0
    scans: int = 0
    field_inputs: dict[str, Any] = field(default_factory=dict)
    I: dict[str, Any] = field(default_factory=dict)
    Q: dict[str, Any] = field(default_factory=dict)
    M: dict[str, Any] = field(default_factory=dict)
    SP: dict[str, Any] = field(default_factory=dict)
    CMD: dict[str, bool] = field(default_factory=dict)
    forces: dict[str, Any] = field(default_factory=dict)
    alarms: dict[str, Alarm] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    last_scan_ms: float = 0.0
    # Hooks: before_scan runs before the input image is taken (signal generators write the
    # field there); after_scan runs once the outputs are written (event publishers read there).
    before_scan: list[Callable[["PLC"], None]] = field(default_factory=list)
    after_scan: list[Callable[["PLC"], None]] = field(default_factory=list)
    _pending: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.tags = {tag.name: tag for tag in self.program.tags}
        for tag in self.program.tags:
            store = {"I": self.field_inputs, "Q": self.Q, "M": self.M, "SP": self.SP, "CMD": self.CMD}[tag.area]
            store[tag.name] = tag.initial
        self.I = dict(self.field_inputs)
        self.program.setup(self)
        self._read_process()

    # ---- the scan cycle -------------------------------------------------
    def scan(self) -> None:
        started = _time.perf_counter()
        dt = self.scan_time
        for hook in self.before_scan:
            hook(self)
        # 1. input image: a frozen copy of the field, with forces applied
        self.I = {name: self.forces.get(name, value) for name, value in self.field_inputs.items()}
        for name in self.CMD:
            self.CMD[name] = name in self._pending
        self._pending.clear()
        # 2. program
        if self.mode == "RUN":
            self.program.execute(self, dt)
        else:
            for name in self.Q:
                self.Q[name] = self.tags[name].initial
        for name in self.Q:
            if name in self.forces:
                self.Q[name] = self.forces[name]
        # 3. outputs to the process, which answers with new sensor values for the next scan
        if self.process is not None:
            self.process.step(dict(self.Q), dt)
            self._read_process()
        self.time += dt
        self.scans += 1
        self.last_scan_ms = (_time.perf_counter() - started) * 1000
        for hook in self.after_scan:
            hook(self)

    def run_for(self, seconds: float) -> int:
        """Advance simulated time. Returns the number of scans executed."""
        count = max(1, round(seconds / self.scan_time))
        for _ in range(count):
            self.scan()
        return count

    def _read_process(self) -> None:
        if self.process is not None:
            for name, value in self.process.sense().items():
                if name in self.field_inputs:
                    self.field_inputs[name] = value

    # ---- helpers the program uses -----------------------------------------
    def read(self, name: str) -> Any:
        for store in (self.I, self.Q, self.M, self.SP, self.CMD):
            if name in store:
                return store[name]
        raise KeyError(name)

    def write(self, name: str, value: Any) -> None:
        area = self.tags[name].area
        if area not in ("Q", "M"):
            raise WriteRejected(f"the program cannot write {area} tag {name}")
        store = self.Q if area == "Q" else self.M
        store[name] = bool(value) if self.tags[name].kind is bool else float(value)

    def alarm(self, name: str, condition: bool, message: str, severity: str = "high") -> None:
        """ISA-18.2 style: an alarm stays listed until it has cleared AND been acknowledged."""
        current = self.alarms.get(name)
        if condition:
            if current is None:
                self.alarms[name] = Alarm(name, message, severity, since=self.time)
                self._log("alarm", name=name, message=message)
            elif not current.active:
                current.active, current.acknowledged, current.since = True, False, self.time
                self._log("alarm", name=name, message=message)
        elif current is not None and current.active:
            current.active = False
            self._log("alarm_cleared", name=name)
            if current.acknowledged:
                del self.alarms[name]

    # ---- what the outside world (HMI, Modbus, an agent) may do ------------
    def set_input(self, name: str, value: Any) -> None:
        """Simulate field wiring: a button pressed, a sensor reading. Not an operator action."""
        if name not in self.field_inputs:
            raise WriteRejected(f"{name} is not an input")
        self.field_inputs[name] = value

    def write_setpoint(self, name: str, value: float, who: str = "operator") -> float:
        tag = self.tags.get(name)
        if tag is None or tag.area != "SP":
            raise WriteRejected(f"{name} is not a setpoint; writable setpoints: {', '.join(self.setpoint_names())}")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise WriteRejected(f"{value!r} is not a number") from None
        if tag.low is not None and value < tag.low or tag.high is not None and value > tag.high:
            raise WriteRejected(f"{name} must be between {tag.low:g} and {tag.high:g} {tag.unit}".strip())
        candidate = {**self.SP, name: value}
        problem = self.program.check_setpoints(candidate)
        if problem:
            raise WriteRejected(problem)
        old = self.SP[name]
        self.SP[name] = value
        self._log("setpoint", who=who, name=name, old=old, new=value)
        return value

    def command(self, name: str, who: str = "operator") -> None:
        tag = self.tags.get(name)
        if tag is None or tag.area != "CMD":
            raise WriteRejected(f"{name} is not a command; available: {', '.join(self.command_names())}")
        self._pending.add(name)
        self._log("command", who=who, name=name)

    def acknowledge(self, who: str = "operator") -> list[str]:
        acked = []
        for name in list(self.alarms):
            alarm = self.alarms[name]
            if not alarm.acknowledged:
                alarm.acknowledged = True
                acked.append(name)
            if not alarm.active:
                del self.alarms[name]
        self._log("acknowledge", who=who, alarms=acked)
        return acked

    def force(self, name: str, value: Any, who: str = "engineer") -> None:
        """Override an I or Q regardless of the program. Commissioning only; always logged."""
        if name not in self.field_inputs and name not in self.Q:
            raise WriteRejected(f"only inputs and outputs can be forced, not {name}")
        self.forces[name] = value
        self._log("force", who=who, name=name, value=value)

    def unforce(self, name: str | None = None, who: str = "engineer") -> None:
        if name is None:
            self.forces.clear()
        else:
            self.forces.pop(name, None)
        self._log("unforce", who=who, name=name or "all")

    def set_mode(self, mode: Literal["RUN", "STOP"], who: str = "engineer") -> None:
        self.mode = mode
        self._log("mode", who=who, mode=mode)

    def setpoint_names(self) -> list[str]:
        return [t.name for t in self.program.tags if t.area == "SP"]

    def command_names(self) -> list[str]:
        return [t.name for t in self.program.tags if t.area == "CMD"]

    def snapshot(self) -> dict[str, Any]:
        def show(store: dict[str, Any]) -> dict[str, Any]:
            return {name: (round(value, 2) if isinstance(value, float) else value) for name, value in store.items()}

        return {
            "program": self.program.name, "mode": self.mode, "time_s": round(self.time, 1), "scans": self.scans,
            "inputs": show(self.I), "outputs": show(self.Q), "memory": show(self.M), "setpoints": show(self.SP),
            "alarms": [alarm.as_dict() for alarm in self.alarms.values()],
            "forced": dict(self.forces),
            "units": {t.name: t.unit for t in self.program.tags if t.unit},
        }

    def _log(self, kind: str, **details: Any) -> None:
        self.events.append({"t": round(self.time, 1), "event": kind, **details})
