"""Simulated field signals: what a test bench or a signal calibrator would inject.

In commissioning you often disconnect the real process and feed the PLC inputs from a
signal source to check the logic ("open-loop test"). A Stimulus does that here: before
each scan it overwrites chosen field inputs with a generated signal, on top of (and
instead of) whatever the process model says.

Signals compose with "+", and are written as text so they fit on a command line:

    const:42                       a constant value
    step:10,0,80                   0 until t=10 s, then 80
    ramp:0,60,20,90                from 20 at t=0 to 90 at t=60, then hold
    sine:50,20,60                  offset 50, amplitude 20, period 60 s
    square:4,0.5,0,1               period 4 s, 50 % duty, low 0, high 1 (a blinking signal)
    pulse:5,0.3                    TRUE for 0.3 s starting at t=5 (a push button press)
    noise:0.5                      Gaussian noise with sigma 0.5 (seeded, repeatable)
    hold:20,3.0                    from t=20, freeze the reading at 3.0 (a stuck sensor)
    ma:12                          a 4-20 mA current, converted to 0-100 % (12 mA = 50 %)
    wirebreak:30                   from t=30 the loop current drops to 0 mA (cut cable)

    sine:50,20,60+noise:0.5        any of them added together

Booleans: a signal on a boolean input is TRUE when the value is >= 0.5.

4-20 mA and NAMUR NE43: a transmitter sends 4 mA at 0 % and 20 mA at 100 %. A live
zero (4 mA, not 0) means a broken wire is distinguishable from an empty tank: below
about 3.6 mA or above 21 mA the reading is a fault, not a measurement. ma_to_percent()
does the scaling, so 0 mA reads as -25 %, which the tank program treats as a sensor fault.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable

from plcsim.runtime import PLC, WriteRejected

Signal = Callable[[float], "float | None"]


def ma_to_percent(milliamps: float) -> float:
    return (milliamps - 4.0) / 16.0 * 100.0


def percent_to_ma(percent: float) -> float:
    return 4.0 + percent / 100.0 * 16.0


def namur_status(milliamps: float) -> str:
    """NE43 bands: <3.6 or >21 mA is a failure, 3.6-3.8 and 20.5-21 are out of range."""
    if milliamps < 3.6 or milliamps > 21.0:
        return "failure"
    if milliamps < 3.8 or milliamps > 20.5:
        return "out_of_range"
    return "ok"


def _numbers(text: str, count: int, kind: str) -> list[float]:
    parts = [p for p in text.split(",") if p.strip()]
    if len(parts) != count:
        raise ValueError(f"{kind} needs {count} numbers, got {text!r}")
    return [float(p) for p in parts]


def parse_signal(spec: str, seed: int = 1) -> Signal:
    """Turn 'sine:50,20,60+noise:0.5' into a function of time."""
    terms = [_term(part.strip(), seed + i) for i, part in enumerate(spec.split("+")) if part.strip()]
    if not terms:
        raise ValueError("empty signal")
    overrides = [term for term in terms if getattr(term, "overrides", False)]
    additive = [term for term in terms if not getattr(term, "overrides", False)]

    def signal(t: float) -> float | None:
        # A failure (wirebreak, hold) wins once it has started. Before that, the plain terms
        # apply; with no plain terms the input is left to the process model.
        for term in overrides:
            value = term(t)
            if value is not None:
                return value
        return sum(term(t) for term in additive) if additive else None

    return signal


def _term(spec: str, seed: int) -> Signal:
    kind, _, args = spec.partition(":")
    kind = kind.strip().lower()
    if kind == "const":
        (value,) = _numbers(args, 1, kind)
        return lambda t: value
    if kind == "step":
        at, before, after = _numbers(args, 3, kind)
        return lambda t: after if t >= at else before
    if kind == "ramp":
        t0, t1, v0, v1 = _numbers(args, 4, kind)
        span = max(t1 - t0, 1e-9)
        return lambda t: v0 if t <= t0 else v1 if t >= t1 else v0 + (v1 - v0) * (t - t0) / span
    if kind == "sine":
        offset, amplitude, period = _numbers(args, 3, kind)
        return lambda t: offset + amplitude * math.sin(2 * math.pi * t / period)
    if kind == "square":
        period, duty, low, high = _numbers(args, 4, kind)
        return lambda t: high if (t % period) < duty * period else low
    if kind == "pulse":
        at, width = _numbers(args, 2, kind)
        return lambda t: 1.0 if at <= t < at + width else 0.0
    if kind == "noise":
        (sigma,) = _numbers(args, 1, kind)
        rng = random.Random(seed)
        return lambda t: rng.gauss(0.0, sigma)
    if kind == "ma":
        (milliamps,) = _numbers(args, 1, kind)
        return lambda t: ma_to_percent(milliamps)
    if kind == "wirebreak":
        (at,) = _numbers(args, 1, kind)
        return _WireBreak(at)
    if kind == "hold":
        at, value = _numbers(args, 2, kind)
        return _Hold(at, value)
    raise ValueError(f"unknown signal {kind!r}; use const, step, ramp, sine, square, pulse, noise, ma, wirebreak or hold")


@dataclass
class _WireBreak:
    """Adds nothing until `at`; afterwards forces the reading to what 0 mA scales to."""

    at: float
    overrides = True

    def __call__(self, t: float) -> float | None:
        return ma_to_percent(0.0) if t >= self.at else None


@dataclass
class _Hold:
    at: float
    value: float
    overrides = True

    def __call__(self, t: float) -> float | None:
        return self.value if t >= self.at else None


@dataclass
class Stimulus:
    """Drives some field inputs from signals. attach() it to a PLC; detach() gives the process back."""

    signals: dict[str, Signal] = field(default_factory=dict)
    history: list[tuple[float, str, float]] = field(default_factory=list)
    record: bool = False

    @classmethod
    def from_specs(cls, specs: list[str], record: bool = False) -> "Stimulus":
        """specs like ['LEVEL=sine:50,20,60', 'START=pulse:1,0.3']"""
        signals = {}
        for item in specs:
            name, sep, spec = item.partition("=")
            if not sep:
                raise ValueError(f"expected NAME=SIGNAL, got {item!r}")
            signals[name.strip().upper()] = parse_signal(spec)
        return cls(signals, record=record)

    def attach(self, plc: PLC) -> "Stimulus":
        for name in self.signals:
            if name not in plc.field_inputs:
                raise WriteRejected(f"{name} is not an input of {plc.program.name}")
        plc.before_scan.append(self)
        return self

    def detach(self, plc: PLC) -> None:
        if self in plc.before_scan:
            plc.before_scan.remove(self)

    def __call__(self, plc: PLC) -> None:
        for name, signal in self.signals.items():
            value = signal(plc.time)
            if value is None:
                continue
            if plc.tags[name].kind is bool:
                plc.field_inputs[name] = value >= 0.5
            else:
                plc.field_inputs[name] = round(value, 3)
            if self.record:
                self.history.append((round(plc.time, 2), name, float(value)))

