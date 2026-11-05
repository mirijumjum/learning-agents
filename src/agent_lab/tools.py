"""Tools: plain Python functions that an agent may decide to call.

A *tool* is how an LLM agent touches the real world. The framework sends the
function's name, docstring and JSON schema (derived from the type hints) to the
model; when the model replies with a "function call", the framework runs the
function and feeds the result back to the model.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_framework import FunctionTool, tool
from pydantic import Field

MAX_OUTPUT_CHARS = 6_000


@dataclass
class PytestResult:
    exit_code: int
    output: str


class PytestRunner:
    """Runs pytest inside one attempt's folder and remembers the last result.

    The runner agent gets ``run_pytest`` as a tool. We *also* keep the real exit
    code here, so the workflow never has to trust the LLM's word on whether the
    tests passed: facts you can measure should be measured, not generated.
    """

    def __init__(self, timeout_seconds: float = 60.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.workdir: Path | None = None
        self.last_result: PytestResult | None = None

    def prepare(self, workdir: Path) -> None:
        self.workdir = workdir
        self.last_result = None

    async def run_pytest(
        self,
        test_file: Annotated[str, Field(description="Test file name inside the workspace, e.g. test_solution.py")] = "test_solution.py",
    ) -> str:
        """Run pytest on a test file in the current workspace and return the exit code and full output."""
        if self.workdir is None:
            return "ERROR: no workspace prepared."
        target = (self.workdir / test_file).resolve()
        if self.workdir.resolve() not in target.parents or not target.is_file():
            return f"ERROR: {test_file!r} is not a test file inside the workspace."

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", target.name,
            cwd=self.workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
            exit_code = proc.returncode if proc.returncode is not None else -1
            output = raw.decode(errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            exit_code, output = -1, f"pytest timed out after {self.timeout_seconds}s"

        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... (output truncated)"
        self.last_result = PytestResult(exit_code=exit_code, output=output)
        return f"exit_code={exit_code}\n{output}"

    def as_tool(self) -> FunctionTool:
        return tool(self.run_pytest, name="run_pytest")
