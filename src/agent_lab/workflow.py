"""Wiring the executors into a workflow graph."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_framework import SupportsChatGetResponse, Workflow, WorkflowBuilder

from agent_lab.agents import create_coder_agent, create_runner_agent, create_tester_agent
from agent_lab.executors import CoderExecutor, QualityGateExecutor, RunnerExecutor, TesterExecutor
from agent_lab.tools import PytestRunner

# role name ("coder" | "tester" | "runner") -> chat client for that agent
ClientFactory = Callable[[str], SupportsChatGetResponse]


def build_workflow(client_factory: ClientFactory, workspace: Path) -> Workflow:
    """Build the graph:

        coder -> tester -> runner -> quality_gate
          ^                              |
          +------------- retry ----------+
    """
    pytest_runner = PytestRunner()

    coder = CoderExecutor(create_coder_agent(client_factory("coder")), workspace)
    tester = TesterExecutor(create_tester_agent(client_factory("tester")))
    runner = RunnerExecutor(create_runner_agent(client_factory("runner"), pytest_runner.as_tool()), pytest_runner)
    gate = QualityGateExecutor()

    return (
        WorkflowBuilder(
            name="write-test-run",
            description="A coder, a tester and a runner agent iterating until the tests pass.",
            start_executor=coder,
        )
        .add_chain([coder, tester, runner, gate])  # the happy path, in order
        .add_edge(gate, coder)  # the feedback loop
        .build()  # validates the graph: connectivity and message types
    )
