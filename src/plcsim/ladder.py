"""A small rung language, close to IEC 61131-3 Structured Text, for writing ladder logic as text.

    # Start/stop with seal-in
    MOTOR := (START OR MOTOR) AND STOP_OK AND NOT OVERLOAD
    TON T_ALARM(IN := LEVEL < 10, PT := T#5s)
    SET FAULT IF T_ALARM.Q
    RESET FAULT IF ACK

Rungs run top to bottom, once per scan, and a rung sees what earlier rungs wrote
in the same scan, just like a real PLC. Nothing here uses eval(): the source is
parsed into a tree and every name is checked against the declared tags, so a
program that writes an input or mentions an unknown tag is rejected before it runs.
That check is what makes it safe to let a language model write rungs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from plcsim.blocks import BLOCK_TYPES

_TOKEN = re.compile(
    r"\s*(?:(?P<time>T#\d+(?:\.\d+)?(?:ms|s|m))|(?P<num>\d+(?:\.\d+)?)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>:=|<=|>=|<>|==|[<>()+\-*/.,=]))",
    re.I,
)
_KEYWORDS = {"AND", "OR", "XOR", "NOT", "TRUE", "FALSE", "SET", "RESET", "IF"}
_WRITABLE_AREAS = {"Q", "M"}
# Inputs each block accepts, and the output that is read when a block is used without ".X".
_BLOCK_INPUTS = {
    "R_TRIG": {"CLK"}, "F_TRIG": {"CLK"}, "TON": {"IN", "PT"}, "TOF": {"IN", "PT"}, "TP": {"IN", "PT"},
    "CTU": {"CU", "R", "PV"}, "CTD": {"CD", "LD", "PV"}, "SR": {"S1", "R"}, "RS": {"S", "R1"},
}
_BLOCK_OUTPUTS = {"Q", "ET", "CV", "Q1"}
_TIMED = {"TON", "TOF", "TP"}


class LadderError(ValueError):
    """The program cannot be compiled. The message says which line and why."""


Node = Callable[["Env"], Any]


@dataclass
class Env:
    read: Callable[[str], Any]
    blocks: dict[str, Any]


@dataclass
class Rung:
    line: int
    source: str
    run: Callable[[Env, Callable[[str, Any], None], float], None]


@dataclass
class Compiled:
    rungs: list[Rung]
    block_types: dict[str, str] = field(default_factory=dict)
    writes: set[str] = field(default_factory=set)

    def new_blocks(self) -> dict[str, Any]:
        return {name: BLOCK_TYPES[kind]() for name, kind in self.block_types.items()}


def _tokens(text: str, line: int) -> list[str]:
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise LadderError(f"line {line}: cannot read {text[pos:].strip()!r}")
        out.append(match.group(match.lastgroup))
        pos = match.end()
    return out


def _seconds(token: str) -> float:
    value = token[2:].lower()
    if value.endswith("ms"):
        return float(value[:-2]) / 1000
    if value.endswith("m"):
        return float(value[:-1]) * 60
    return float(value[:-1])


class _Parser:
    def __init__(self, tokens: list[str], line: int, names: dict[str, str], blocks: dict[str, str]) -> None:
        self.tokens, self.pos, self.line, self.names, self.blocks = tokens, 0, line, names, blocks

    def fail(self, message: str) -> LadderError:
        return LadderError(f"line {self.line}: {message}")

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, expected: str | None = None, what: str | None = None) -> str:
        token = self.peek()
        if token is None:
            raise self.fail(f"expected {what or repr(expected or 'more')} at end of line")
        if expected and token.upper() != expected:
            raise self.fail(f"expected {expected!r}, found {token!r}")
        self.pos += 1
        return token

    def done(self) -> None:
        if self.peek() is not None:
            raise self.fail(f"unexpected {self.peek()!r}")

    # expr := and { (OR|XOR) and }
    def expr(self) -> Node:
        node = self.conj()
        while (self.peek() or "").upper() in ("OR", "XOR"):
            op = self.take().upper()
            left, right = node, self.conj()
            node = (lambda a, b: lambda e: bool(a(e)) or bool(b(e)))(left, right) if op == "OR" else (lambda a, b: lambda e: bool(a(e)) != bool(b(e)))(left, right)
        return node

    def conj(self) -> Node:
        node = self.neg()
        while (self.peek() or "").upper() == "AND":
            self.take()
            node = (lambda a, b: lambda e: bool(a(e)) and bool(b(e)))(node, self.neg())
        return node

    def neg(self) -> Node:
        if (self.peek() or "").upper() == "NOT":
            self.take()
            inner = self.neg()
            return lambda e: not bool(inner(e))
        return self.compare()

    def compare(self) -> Node:
        left = self.sum()
        ops = {"<": lambda a, b: a < b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
               "=": lambda a, b: a == b, "==": lambda a, b: a == b, "<>": lambda a, b: a != b}
        if self.peek() in ops:
            fn, right = ops[self.take()], self.sum()
            return lambda e: fn(left(e), right(e))
        return left

    def sum(self) -> Node:
        node = self.product()
        while self.peek() in ("+", "-"):
            op, right = self.take(), self.product()
            node = (lambda a, b: lambda e: a(e) + b(e))(node, right) if op == "+" else (lambda a, b: lambda e: a(e) - b(e))(node, right)
        return node

    def product(self) -> Node:
        node = self.atom()
        while self.peek() in ("*", "/"):
            op, right = self.take(), self.atom()
            if op == "*":
                node = (lambda a, b: lambda e: a(e) * b(e))(node, right)
            else:
                node = (lambda a, b: lambda e: a(e) / b(e) if b(e) else 0.0)(node, right)
        return node

    def atom(self) -> Node:
        token = self.take(what="a value")
        upper = token.upper()
        if token == "(":
            node = self.expr()
            self.take(")")
            return node
        if token == "-":
            inner = self.atom()
            return lambda e: -inner(e)
        if upper in ("TRUE", "FALSE"):
            value = upper == "TRUE"
            return lambda e: value
        if upper.startswith("T#"):
            value = _seconds(token)
            return lambda e: value
        if re.fullmatch(r"\d+(\.\d+)?", token):
            number = float(token)
            return lambda e: number
        if upper in _KEYWORDS:
            raise self.fail(f"{token!r} cannot be used as a value")
        name = upper
        if self.peek() == ".":
            self.take()
            member = self.take(what="a block output").upper()
            if name not in self.blocks:
                raise self.fail(f"{token!r} is not a function block declared above")
            if member not in _BLOCK_OUTPUTS:
                raise self.fail(f"{token}.{member} does not exist (use Q, ET, CV or Q1)")
            return lambda e: getattr(e.blocks[name], member)
        if name in self.blocks:
            output = "Q1" if self.blocks[name] in ("SR", "RS") else "Q"
            return lambda e: getattr(e.blocks[name], output)
        if name not in self.names:
            raise self.fail(f"unknown tag {token!r}")
        return lambda e: e.read(name)


def compile_program(source: str, tags: dict[str, str]) -> Compiled:
    """tags maps each declared tag name to its area (I, Q, M, SP or CMD)."""
    names = {name.upper(): area for name, area in tags.items()}
    lines: list[tuple[int, str]] = []
    for number, raw in enumerate(source.splitlines(), start=1):
        text = re.split(r"(?<![Tt])#|//", raw, maxsplit=1)[0].strip()
        lines += [(number, part.strip()) for part in text.split(";") if part.strip()]
    # First pass: every block instance, so a rung may read a timer that is called further down
    # (it then sees the value from the previous scan, exactly as in a real PLC).
    block_types: dict[str, str] = {}
    for number, text in lines:
        words = text.split(None, 2)
        if words[0].upper() in BLOCK_TYPES:
            instance = words[1].split("(")[0].upper() if len(words) > 1 else ""
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", instance):
                raise LadderError(f"line {number}: {words[0]} needs an instance name, for example {words[0].upper()} T1(...)")
            if instance in names or instance in _KEYWORDS:
                raise LadderError(f"line {number}: {instance} is already a tag or keyword")
            if instance in block_types:
                raise LadderError(f"line {number}: {instance} is declared twice; call each block instance once per scan")
            block_types[instance] = words[0].upper()
    rungs: list[Rung] = []
    writes: set[str] = set()
    for number, text in lines:
        rungs.append(_compile_rung(text, number, names, block_types, writes))
    if not rungs:
        raise LadderError("the program has no rungs")
    return Compiled(rungs, block_types, writes)


def _target(parser: _Parser, names: dict[str, str]) -> str:
    token = parser.take(what="a tag to write")
    name = token.upper()
    if name not in names:
        raise parser.fail(f"unknown tag {token!r}")
    if names[name] not in _WRITABLE_AREAS:
        raise parser.fail(f"{token} is an {names[name]} tag; rungs may only write outputs (Q) and memory (M)")
    return name


def _compile_rung(text: str, line: int, names: dict[str, str], blocks: dict[str, str], writes: set[str]) -> Rung:
    tokens = _tokens(text, line)
    parser = _Parser(tokens, line, names, blocks)
    head = tokens[0].upper()

    if head in ("SET", "RESET"):
        parser.take()
        target = _target(parser, names)
        parser.take("IF")
        condition = parser.expr()
        parser.done()
        writes.add(target)
        value = head == "SET"

        def run_coil(env: Env, write: Callable[[str, Any], None], dt: float) -> None:
            if condition(env):
                write(target, value)

        return Rung(line, text, run_coil)

    if head in BLOCK_TYPES:
        parser.take()
        instance = parser.take(what="an instance name").upper()
        parser.take("(")
        args: dict[str, Node] = {}
        while parser.peek() != ")":
            arg = parser.take(what="an input name").upper()
            if arg not in _BLOCK_INPUTS[head]:
                raise parser.fail(f"{head} has no input {arg} (inputs: {', '.join(sorted(_BLOCK_INPUTS[head]))})")
            parser.take(":=")
            args[arg] = parser.expr()
            if parser.peek() == ",":
                parser.take()
        parser.take(")")
        parser.done()
        if head in _TIMED and "PT" not in args:
            raise parser.fail(f"{head} needs PT, for example PT := T#5s")

        def run_block(env: Env, write: Callable[[str, Any], None], dt: float) -> None:
            values = {key: node(env) for key, node in args.items()}
            if "PV" in values:
                values["PV"] = int(values["PV"])
            env.blocks[instance](**values, dt=dt)

        return Rung(line, text, run_block)

    target = _target(parser, names)
    parser.take(":=")
    value_node = parser.expr()
    parser.done()
    writes.add(target)

    def run_assign(env: Env, write: Callable[[str, Any], None], dt: float) -> None:
        write(target, value_node(env))

    return Rung(line, text, run_assign)
