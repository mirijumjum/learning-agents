"""Agent definitions.

An ``Agent`` in Agent Framework is: a chat client (the LLM connection) +
instructions (its system prompt / persona) + optional tools. Calling
``await agent.run("...")`` performs one full turn, including any tool calls
the model decides to make, and returns an ``AgentResponse``.
"""

from __future__ import annotations

from agent_framework import Agent, FunctionTool, SupportsChatGetResponse

CODER_INSTRUCTIONS = """\
You are a senior Python developer.
Write a single Python module that implements exactly what the user asks for.
Rules:
- Standard library only. Use type hints and a short docstring on every public function.
- No tests, no print statements, no `if __name__ == "__main__"` block.
- If you receive feedback from a failed test run, fix the root cause.
- Reply with ONE fenced ```python code block and nothing else."""

TESTER_INSTRUCTIONS = """\
You are a meticulous QA engineer who writes pytest tests.
You receive a feature description and the public API (signatures + docstrings) of a module named `solution`.
You do NOT see the implementation: test the specification, not the code.
Rules:
- Import from `solution`, e.g. `from solution import my_function`.
- Cover normal cases, edge cases and invalid input when the spec defines it. 4 to 10 tests.
- Use plain `assert` statements and `pytest.raises` where relevant. Use `pytest.mark.parametrize` when it helps.
- If a previous run failed because a test contradicted the spec, fix that test.
- Reply with ONE fenced ```python code block and nothing else."""

RUNNER_INSTRUCTIONS = """\
You are a test runner. You MUST call the `run_pytest` tool exactly once to run the tests.
Then reply with a short report (2-4 sentences): how many tests passed/failed and,
if anything failed, the most likely root cause. Never invent results: only report what the tool returned."""


def create_coder_agent(client: SupportsChatGetResponse) -> Agent:
    return Agent(client, CODER_INSTRUCTIONS, name="coder", description="Writes the implementation.")


def create_tester_agent(client: SupportsChatGetResponse) -> Agent:
    return Agent(client, TESTER_INSTRUCTIONS, name="tester", description="Writes pytest tests from the spec.")


def create_runner_agent(client: SupportsChatGetResponse, run_pytest: FunctionTool) -> Agent:
    return Agent(client, RUNNER_INSTRUCTIONS, name="runner", description="Runs the tests.", tools=[run_pytest])
