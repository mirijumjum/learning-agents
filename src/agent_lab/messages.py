"""Typed messages that travel along the workflow edges.

In Agent Framework a workflow is a graph of *executors*. Executors talk to each
other by sending messages, and the framework routes each message to the
handler whose input type matches. Using small dataclasses (instead of plain
strings) makes the graph self-documenting and lets the framework validate that
connected executors actually speak the same "language".

    FeatureRequest --> [coder] --> CodeDraft --> [tester] --> GeneratedTests
        ^                                                          |
        |                                                          v
    [quality_gate] <------------- RunReport <-------------- [runner]
        |
        +--> FinalResult (workflow output)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class FeatureRequest:
    """What the user wants built. Also used to ask the coder for a retry."""

    description: str
    attempt: int = 1
    max_attempts: int = 3
    # Filled in on retries so the coder can learn from the previous failure.
    feedback: str | None = None
    previous_code: str | None = None
    previous_tests: str | None = None


@dataclass
class CodeDraft:
    """Output of the coder agent: an implementation saved to disk."""

    request: FeatureRequest
    source: str
    public_api: str  # signatures + docstrings only, handed to the tester
    workdir: Path

    @property
    def path(self) -> Path:
        return self.workdir / "solution.py"


@dataclass
class GeneratedTests:
    """Output of the tester agent: a pytest file saved next to the code."""

    draft: CodeDraft
    source: str

    @property
    def path(self) -> Path:
        return self.draft.workdir / "test_solution.py"

    @property
    def count(self) -> int:
        return sum(1 for line in self.source.splitlines() if line.lstrip().startswith(("def test_", "async def test_")))


@dataclass
class RunReport:
    """Output of the runner agent: the *real* pytest result plus the agent's summary."""

    tests: GeneratedTests
    exit_code: int
    output: str
    summary: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass
class FinalResult:
    """What the whole workflow yields at the end."""

    success: bool
    attempts: int
    description: str
    solution_path: Path
    tests_path: Path
    summary: str
