"""Executors: the nodes of the workflow graph.

An ``Executor`` is a unit of work with one or more ``@handler`` methods. The
type of a handler's first argument says which messages it accepts; the
``WorkflowContext[Out, WorkflowOut]`` annotation says what it may send to the
next node (``ctx.send_message``) and what it may emit as the final workflow
output (``ctx.yield_output``).

Three executors wrap an LLM agent (coder, tester, runner). The quality gate is
plain deterministic Python: a workflow mixes AI steps and regular code freely.
"""

from __future__ import annotations

from pathlib import Path

from agent_framework import Agent, Executor, WorkflowContext, handler

from agent_lab.code_utils import describe_public_api, extract_python_code
from agent_lab.messages import CodeDraft, FeatureRequest, FinalResult, GeneratedTests, RunReport
from agent_lab.tools import PytestRunner

# An empty pytest.ini makes each attempt folder its own pytest rootdir,
# isolating it from any pytest configuration in parent directories.
_PYTEST_INI = "[pytest]\n"


class CoderExecutor(Executor):
    """FeatureRequest -> CodeDraft. Asks the coder agent for an implementation and saves it."""

    def __init__(self, agent: Agent, workspace: Path) -> None:
        super().__init__(id="coder")
        self.agent = agent
        self.workspace = workspace

    @handler
    async def write_code(self, request: FeatureRequest, ctx: WorkflowContext[CodeDraft]) -> None:
        prompt = f"Feature to implement:\n{request.description}"
        if request.feedback:
            prompt += (
                f"\n\nPREVIOUS ATTEMPT FAILED (attempt {request.attempt - 1}).\n"
                f"Your previous code:\n```python\n{request.previous_code}```\n"
                f"The tests:\n```python\n{request.previous_tests}```\n"
                f"Test run report:\n{request.feedback}\n"
                "Fix the implementation."
            )

        response = await self.agent.run(prompt)
        source = extract_python_code(response.text)

        workdir = self.workspace / f"attempt_{request.attempt}"
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "pytest.ini").write_text(_PYTEST_INI)
        draft = CodeDraft(request=request, source=source, public_api=describe_public_api(source), workdir=workdir)
        draft.path.write_text(source)

        await ctx.send_message(draft)


class TesterExecutor(Executor):
    """CodeDraft -> GeneratedTests. Asks the tester agent for pytest tests and saves them."""

    def __init__(self, agent: Agent) -> None:
        super().__init__(id="tester")
        self.agent = agent

    @handler
    async def write_tests(self, draft: CodeDraft, ctx: WorkflowContext[GeneratedTests]) -> None:
        request = draft.request
        prompt = (
            f"Feature description:\n{request.description}\n\n"
            f"Public API of module `solution`:\n```python\n{draft.public_api}\n```"
        )
        if request.feedback:
            prompt += (
                f"\n\nThe previous test run failed. Previous tests:\n```python\n{request.previous_tests}```\n"
                f"Report:\n{request.feedback}\n"
                "Keep tests that are correct according to the spec; fix only tests that contradict it."
            )

        response = await self.agent.run(prompt)
        tests = GeneratedTests(draft=draft, source=extract_python_code(response.text))
        tests.path.write_text(tests.source)

        await ctx.send_message(tests)


class RunnerExecutor(Executor):
    """GeneratedTests -> RunReport. The runner agent executes pytest through a tool."""

    def __init__(self, agent: Agent, pytest_runner: PytestRunner) -> None:
        super().__init__(id="runner")
        self.agent = agent
        self.pytest_runner = pytest_runner

    @handler
    async def run_tests(self, tests: GeneratedTests, ctx: WorkflowContext[RunReport]) -> None:
        self.pytest_runner.prepare(tests.draft.workdir)
        response = await self.agent.run(f"Run the tests in `{tests.path.name}` and report the result.")
        summary = response.text.strip()

        result = self.pytest_runner.last_result
        if result is None:
            # The model ignored its instructions and never called the tool.
            # Don't trust a made-up answer: run the tests ourselves.
            await self.pytest_runner.run_pytest(tests.path.name)
            result = self.pytest_runner.last_result
            summary = "(The runner agent did not call the tool; pytest was run directly.)"
        assert result is not None

        await ctx.send_message(RunReport(tests=tests, exit_code=result.exit_code, output=result.output, summary=summary))


class QualityGateExecutor(Executor):
    """RunReport -> FinalResult (done) or FeatureRequest (loop back to the coder).

    This is where the graph's cycle lives: the edge quality_gate -> coder turns a
    straight pipeline into an iterative "write, test, fix" loop.
    """

    def __init__(self) -> None:
        super().__init__(id="quality_gate")

    @handler
    async def decide(self, report: RunReport, ctx: WorkflowContext[FeatureRequest, FinalResult]) -> None:
        tests = report.tests
        draft = tests.draft
        request = draft.request

        if report.passed or request.attempt >= request.max_attempts:
            await ctx.yield_output(
                FinalResult(
                    success=report.passed,
                    attempts=request.attempt,
                    description=request.description,
                    solution_path=draft.path,
                    tests_path=tests.path,
                    summary=report.summary,
                )
            )
            return

        await ctx.send_message(
            FeatureRequest(
                description=request.description,
                attempt=request.attempt + 1,
                max_attempts=request.max_attempts,
                feedback=f"{report.summary}\n\nRaw pytest output:\n{report.output}",
                previous_code=draft.source,
                previous_tests=tests.source,
            )
        )
