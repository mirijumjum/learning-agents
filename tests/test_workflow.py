"""End-to-end tests of the workflow using the offline mock client."""

import asyncio

from agent_lab.messages import FeatureRequest, FinalResult
from agent_lab.mock_client import DEMO_PROMPTS, MockChatClient
from agent_lab.workflow import build_workflow


def _run_workflow(tmp_path, description, max_attempts=3):
    workflow = build_workflow(lambda role: MockChatClient(role, latency_seconds=0), tmp_path)
    result = asyncio.run(workflow.run(FeatureRequest(description=description, max_attempts=max_attempts)))
    outputs = result.get_outputs()
    assert len(outputs) == 1 and isinstance(outputs[0], FinalResult)
    return outputs[0]


def test_palindrome_fails_first_then_fixes_itself(tmp_path):
    final = _run_workflow(tmp_path, DEMO_PROMPTS["palindrome"])
    assert final.success is True
    assert final.attempts == 2
    assert (tmp_path / "attempt_1" / "solution.py").exists()  # the buggy attempt is kept for inspection
    assert final.solution_path == tmp_path / "attempt_2" / "solution.py"


def test_fizzbuzz_passes_on_first_attempt(tmp_path):
    final = _run_workflow(tmp_path, DEMO_PROMPTS["fizzbuzz"])
    assert (final.success, final.attempts) == (True, 1)


def test_gives_up_after_max_attempts(tmp_path):
    final = _run_workflow(tmp_path, DEMO_PROMPTS["palindrome"], max_attempts=1)
    assert (final.success, final.attempts) == (False, 1)
