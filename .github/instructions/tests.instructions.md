---
applyTo: "tests/**/*.py"
---

# Tests

- Use plain pytest functions. Name files `test_<area>.py` and tests `test_<behaviour>`.
- Import the real modules from `agent_lab` or `devassist`. Do not mock the code under test; mock only the edges (network, models, clocks) the way existing tests do (`mock_llm.py`, the local GitLab stand-in, `monkeypatch`).
- Tests must run offline and in under a few seconds each. No real API keys, no Ollama, no network outside 127.0.0.1.
- Use expected values you can work out by hand. Use `pytest.approx` for floats.
- Never delete, skip or weaken an existing test to make the suite pass. If production code looks wrong, report it in the pull request instead of "fixing" the test.
- Never overwrite a test written by a person. If a file name is taken, add `_ai` to the new file name.
