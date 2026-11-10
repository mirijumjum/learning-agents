"""The openai-compatible profile (vLLM, Ollama's /v1, llama.cpp...) against a local stand-in for the model server."""

import asyncio
import json

import pytest
from agent_framework import tool
from aiohttp import web

from devassist.audit import AuditLog
from devassist.cli import main
from devassist.config import ConfigError, Settings
from devassist.llm import LLM, TIER_BY_ROLE, model_name

ENV = {
    "DEVASSIST_PROFILE": "openai-compatible",
    "OPENAI_COMPATIBLE_BASE_URL": "http://gpu-server:8000/v1/",
    "OPENAI_COMPATIBLE_MODEL_STRONG": "Qwen/Qwen3-30B-A3B-Instruct-2507",
    "OPENAI_COMPATIBLE_MODEL_FAST": "Qwen/Qwen3-8B",
}


def _settings(base_url: str, tmp_path, **overrides) -> Settings:
    values = dict(
        profile="openai-compatible", openai_compat_base_url=base_url, openai_compat_api_key="secret-key",
        openai_compat_model_strong="strong-model", openai_compat_model_fast="fast-model", audit_path=tmp_path / "audit.jsonl",
    )
    values.update(overrides)
    return Settings(**values)


class StubModelServer:
    """Records each request and answers like vLLM: optionally one tool call first, then text."""

    def __init__(self, tool_call: dict | None = None) -> None:
        self.requests: list[tuple[str, str | None, dict]] = []
        self.tool_call = tool_call

    async def completions(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append((request.path, request.headers.get("Authorization"), body))
        if self.tool_call and len(self.requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": self.tool_call["name"], "arguments": json.dumps(self.tool_call["arguments"])}},
            ]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "<think>internal</think>hello"}
            finish = "stop"
        return web.json_response({
            "id": "1", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000, "total_tokens": 1_100_000},
        })

    async def models(self, request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "strong-model", "object": "model"}, {"id": "fast-model", "object": "model"}]})


async def _serve(stub: StubModelServer):
    app = web.Application()
    app.router.add_post("/v1/chat/completions", stub.completions)
    app.router.add_get("/v1/models", stub.models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"


def _ask(tmp_path, stub: StubModelServer, role="test", deliberate=False, tools=None, **overrides):
    async def run():
        runner, base_url = await _serve(stub)
        settings = _settings(base_url, tmp_path, **overrides)
        audit = AuditLog(settings.audit_path)
        try:
            answer = await LLM(settings, audit).ask(role, "You help developers.", "Say hello", tools=tools, deliberate=deliberate)
        finally:
            await runner.cleanup()
        return answer, audit

    return asyncio.run(run())


def test_configuration_comes_from_the_environment():
    settings = Settings.from_env(ENV)
    assert settings.openai_compat_base_url == "http://gpu-server:8000/v1"
    assert settings.openai_compat_api_key == "" and settings.openai_compat_reasoning_mode == "effort"
    expected = {"test": "Qwen/Qwen3-30B-A3B-Instruct-2507", "docs": "Qwen/Qwen3-30B-A3B-Instruct-2507", "trace_complex": "Qwen/Qwen3-30B-A3B-Instruct-2507",
                "git": "Qwen/Qwen3-8B", "trace": "Qwen/Qwen3-8B", "router": "Qwen/Qwen3-8B"}
    assert {role: model_name(settings, TIER_BY_ROLE[role]) for role in expected} == expected


@pytest.mark.parametrize("missing", ["OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_MODEL_STRONG", "OPENAI_COMPATIBLE_MODEL_FAST"])
def test_missing_settings_are_named(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        Settings.from_env(env)


def test_unknown_reasoning_mode_is_rejected():
    with pytest.raises(ConfigError, match="OPENAI_COMPATIBLE_REASONING_MODE"):
        Settings.from_env({**ENV, "OPENAI_COMPATIBLE_REASONING_MODE": "maybe"})


def test_request_shape_and_audit(tmp_path):
    stub = StubModelServer()
    answer, audit = _ask(tmp_path, stub)
    assert answer == "hello"  # <think> blocks never reach the user
    path, auth, body = stub.requests[0]
    assert path == "/v1/chat/completions" and auth == "Bearer secret-key"
    assert body["model"] == "strong-model"
    assert body["max_completion_tokens"] == 4000
    assert body["reasoning_effort"] == "none"  # small local models: reasoning off unless asked for
    assert "chat_template_kwargs" not in body
    summary = audit.summary()
    assert summary["cost_usd"] == 0.0  # own hardware: no per-token bill
    assert round(summary["estimated_azure_cost_usd"], 3) == 2.475  # still shows what Azure would have charged


def test_fast_roles_use_the_fast_model(tmp_path):
    stub = StubModelServer()
    _ask(tmp_path, stub, role="router")
    assert stub.requests[0][2]["model"] == "fast-model"


def test_no_api_key_still_sends_a_placeholder(tmp_path):
    stub = StubModelServer()
    _ask(tmp_path, stub, openai_compat_api_key="")
    assert stub.requests[0][1] == "Bearer not-needed"


def test_explicit_effort_is_forwarded_for_gpt_oss(tmp_path):
    stub = StubModelServer()
    _ask(tmp_path, stub, reasoning_effort="low")
    assert stub.requests[0][2]["reasoning_effort"] == "low"


def test_thinking_flag_mode_turns_qwen3_thinking_off_and_on(tmp_path):
    stub = StubModelServer()
    _ask(tmp_path, stub, openai_compat_reasoning_mode="thinking-flag")
    body = stub.requests[0][2]
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and "reasoning_effort" not in body

    stub = StubModelServer()
    _ask(tmp_path, stub, openai_compat_reasoning_mode="thinking-flag", deliberate=True)
    body = stub.requests[0][2]
    assert body["chat_template_kwargs"] == {"enable_thinking": True}  # the test-or-code judgement gets to think
    assert body["max_completion_tokens"] == 12000


def test_omit_mode_sends_no_reasoning_fields(tmp_path):
    stub = StubModelServer()
    _ask(tmp_path, stub, openai_compat_reasoning_mode="omit")
    body = stub.requests[0][2]
    assert "reasoning_effort" not in body and "chat_template_kwargs" not in body


def test_tool_calls_round_trip(tmp_path):
    calls = []

    def pipeline_status(pipeline_id: int) -> str:
        """Status of a pipeline."""
        calls.append(pipeline_id)
        return "failed"

    stub = StubModelServer(tool_call={"name": "pipeline_status", "arguments": {"pipeline_id": 5512}})
    answer, _ = _ask(tmp_path, stub, role="git", tools=[tool(pipeline_status, name="pipeline_status")])
    assert calls == [5512] and answer == "hello"
    first, second = stub.requests[0][2], stub.requests[1][2]
    assert first["tools"][0]["function"]["name"] == "pipeline_status"
    assert any(m.get("role") == "tool" and "failed" in str(m.get("content")) for m in second["messages"])


def test_doctor_checks_the_model_server(tmp_path, monkeypatch, capsys):
    stub = StubModelServer()

    async def run():
        runner, base_url = await _serve(stub)
        monkeypatch.setenv("DEVASSIST_PROFILE", "openai-compatible")
        monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", base_url)
        monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL_STRONG", "strong-model")
        monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL_FAST", "missing-model")
        monkeypatch.setenv("GITLAB_URL", "http://127.0.0.1:9")
        try:
            return await asyncio.to_thread(main, ["doctor"])
        finally:
            await runner.cleanup()

    monkeypatch.chdir(tmp_path)
    asyncio.run(run())
    out = capsys.readouterr().out
    assert "[ok] model server" in out and "[ok] model strong-model" in out
    assert "[FAIL] model missing-model" in out and "fast-model" in out


def test_fast_model_can_live_on_another_server(tmp_path):
    strong, fast = StubModelServer(), StubModelServer()

    async def run():
        (strong_runner, strong_url), (fast_runner, fast_url) = await _serve(strong), await _serve(fast)
        settings = _settings(strong_url, tmp_path, openai_compat_base_url_fast=fast_url)
        llm = LLM(settings, AuditLog(settings.audit_path))
        try:
            await llm.ask("router", "Route.", "hi")
            await llm.ask("docs", "Document.", "hi")
        finally:
            await strong_runner.cleanup()
            await fast_runner.cleanup()

    asyncio.run(run())
    assert [r[2]["model"] for r in fast.requests] == ["fast-model"]
    assert [r[2]["model"] for r in strong.requests] == ["strong-model"]


def test_pointed_at_ollama_it_sends_what_the_ollama_profile_sends(tmp_path):
    """So the release evals of the ollama profile carry over to openai-compatible on Ollama."""
    generic, native = StubModelServer(), StubModelServer()

    async def run():
        (generic_runner, generic_url), (native_runner, native_url) = await _serve(generic), await _serve(native)
        compat = _settings(generic_url, tmp_path, openai_compat_model_strong="qwen3:8b", openai_compat_model_fast="qwen3:4b")
        ollama = Settings(profile="ollama", ollama_base_url=native_url, audit_path=tmp_path / "audit2.jsonl")
        try:
            for settings in (compat, ollama):
                llm = LLM(settings, AuditLog(settings.audit_path))
                await llm.ask("router", "Route.", "hi")
                await llm.ask("test", "Judge.", "hi", deliberate=True)
        finally:
            await generic_runner.cleanup()
            await native_runner.cleanup()

    asyncio.run(run())
    assert [r[2] for r in generic.requests] == [r[2] for r in native.requests]
