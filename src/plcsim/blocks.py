"""IEC 61131-3 standard function blocks.

Each block keeps its own state between scans, exactly like an instance of a
function block in a real PLC. Call it once per scan with the elapsed scan time
(dt, in seconds), then read its outputs (.Q, .ET, .CV).
"""

from __future__ import annotations

# Ten scans of 0.1 s add up to 0.9999999999999999 s in floating point, so a 1 s timer would
# need an eleventh scan. Real PLCs count time in integer milliseconds; here we allow for
# the rounding instead.
_EPS = 1e-9


class R_TRIG:
    """Rising edge: Q is TRUE for exactly one scan when CLK goes FALSE -> TRUE."""

    def __init__(self) -> None:
        self.Q = False
        self._last = False

    def __call__(self, CLK: bool, dt: float = 0.0) -> bool:
        self.Q = bool(CLK) and not self._last
        self._last = bool(CLK)
        return self.Q


class F_TRIG:
    """Falling edge: Q is TRUE for exactly one scan when CLK goes TRUE -> FALSE."""

    def __init__(self) -> None:
        self.Q = False
        self._last = False

    def __call__(self, CLK: bool, dt: float = 0.0) -> bool:
        self.Q = not bool(CLK) and self._last
        self._last = bool(CLK)
        return self.Q


class TON:
    """On-delay timer: Q goes TRUE once IN has been TRUE for PT seconds."""

    def __init__(self, PT: float = 0.0) -> None:
        self.PT = PT
        self.ET = 0.0
        self.Q = False

    def __call__(self, IN: bool, dt: float, PT: float | None = None) -> bool:
        if PT is not None:
            self.PT = PT
        if IN:
            self.ET = min(self.ET + dt, self.PT)
        else:
            self.ET = 0.0
        self.Q = bool(IN) and self.ET >= self.PT - _EPS
        return self.Q


class TOF:
    """Off-delay timer: Q follows IN up, and stays TRUE for PT seconds after IN drops."""

    def __init__(self, PT: float = 0.0) -> None:
        self.PT = PT
        self.ET = 0.0
        self.Q = False

    def __call__(self, IN: bool, dt: float, PT: float | None = None) -> bool:
        if PT is not None:
            self.PT = PT
        if IN:
            self.ET = 0.0
            self.Q = True
        elif self.Q:
            self.ET = min(self.ET + dt, self.PT)
            self.Q = self.ET < self.PT - _EPS
        return self.Q


class TP:
    """Pulse timer: a rising edge on IN gives a pulse of exactly PT seconds."""

    def __init__(self, PT: float = 0.0) -> None:
        self.PT = PT
        self.ET = 0.0
        self.Q = False
        self._last = False

    def __call__(self, IN: bool, dt: float, PT: float | None = None) -> bool:
        if PT is not None:
            self.PT = PT
        if IN and not self._last and not self.Q:
            self.Q, self.ET = True, 0.0
        elif self.Q:
            self.ET = min(self.ET + dt, self.PT)
            self.Q = self.ET < self.PT - _EPS
        elif not IN:
            self.ET = 0.0
        self._last = bool(IN)
        return self.Q


class CTU:
    """Up counter: counts rising edges of CU; Q is TRUE when CV >= PV. R resets."""

    def __init__(self, PV: int = 0) -> None:
        self.PV = PV
        self.CV = 0
        self.Q = False
        self._edge = R_TRIG()

    def __call__(self, CU: bool, R: bool = False, PV: int | None = None, dt: float = 0.0) -> bool:
        if PV is not None:
            self.PV = PV
        edge = self._edge(CU)
        if R:
            self.CV = 0
        elif edge:
            self.CV += 1
        self.Q = self.CV >= self.PV
        return self.Q


class CTD:
    """Down counter: LD loads PV, each rising edge of CD decrements; Q is TRUE when CV <= 0."""

    def __init__(self, PV: int = 0) -> None:
        self.PV = PV
        self.CV = 0
        self.Q = True
        self._edge = R_TRIG()

    def __call__(self, CD: bool, LD: bool = False, PV: int | None = None, dt: float = 0.0) -> bool:
        if PV is not None:
            self.PV = PV
        edge = self._edge(CD)
        if LD:
            self.CV = self.PV
        elif edge and self.CV > 0:
            self.CV -= 1
        self.Q = self.CV <= 0
        return self.Q


class SR:
    """Set-dominant bistable: Q1 := S1 OR (Q1 AND NOT R)."""

    def __init__(self) -> None:
        self.Q1 = False

    def __call__(self, S1: bool, R: bool, dt: float = 0.0) -> bool:
        self.Q1 = bool(S1) or (self.Q1 and not R)
        return self.Q1


class RS:
    """Reset-dominant bistable: Q1 := NOT R1 AND (S OR Q1). Use it for anything safety-related."""

    def __init__(self) -> None:
        self.Q1 = False

    def __call__(self, S: bool, R1: bool, dt: float = 0.0) -> bool:
        self.Q1 = not R1 and (bool(S) or self.Q1)
        return self.Q1


BLOCK_TYPES = {cls.__name__: cls for cls in (R_TRIG, F_TRIG, TON, TOF, TP, CTU, CTD, SR, RS)}
