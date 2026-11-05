import asyncio

from agent_lab.tools import PytestRunner


def _run(coro):
    return asyncio.run(coro)


def test_runs_pytest_and_records_real_exit_code(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert 1 + 1 == 3\n")
    runner = PytestRunner()
    runner.prepare(tmp_path)

    assert "exit_code=0" in _run(runner.run_pytest("test_ok.py"))
    assert runner.last_result.exit_code == 0

    _run(runner.run_pytest("test_bad.py"))
    assert runner.last_result.exit_code == 1
    assert "1 failed" in runner.last_result.output


def test_refuses_paths_outside_the_workspace(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    (tmp_path / "test_outside.py").write_text("def test_x():\n    pass\n")
    runner = PytestRunner()
    runner.prepare(workdir)

    assert _run(runner.run_pytest("../test_outside.py")).startswith("ERROR")
    assert runner.last_result is None
