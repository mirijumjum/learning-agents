from agent_lab.cli import main


def test_graph_contains_the_feedback_loop(capsys):
    assert main(["graph"]) == 0
    out = capsys.readouterr().out
    assert "runner --> quality_gate" in out
    assert "quality_gate --> coder" in out


def test_mock_rejects_unknown_requests(capsys):
    assert main(["run", "build me a web server"]) == 2
    assert "only has scripts" in capsys.readouterr().out


def test_missing_api_key_is_reported(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["run", "x", "--provider", "openai"]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().out


def test_run_celsius_demo(tmp_path, capsys):
    assert main(["run", "celsius to fahrenheit", "-w", str(tmp_path)]) == 0
    assert "Done in 1 attempt" in capsys.readouterr().out
