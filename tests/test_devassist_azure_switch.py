"""Proves the move to Azure is configuration only, against a local stand-in for the Azure endpoint."""

import asyncio

from aiohttp import web

from devassist.audit import AuditLog
from devassist.config import Settings
from devassist.llm import LLM, TIER_BY_ROLE, model_name


def _azure(endpoint="https://x.openai.azure.com/", **overrides):
    return Settings(profile="azure", azure_endpoint=endpoint, azure_api_key="test-key", **overrides)


def test_each_agent_gets_the_model_the_design_assigns():
    settings = _azure()
    expected = {"test": "gpt-5", "docs": "gpt-5", "trace_complex": "gpt-5", "git": "gpt-5-mini", "trace": "gpt-5-mini", "router": "gpt-5-mini"}
    assert {role: model_name(settings, TIER_BY_ROLE[role]) for role in expected} == expected


def test_free_profile_mirrors_the_same_split():
    settings = Settings(profile="ollama")
    assert model_name(settings, "strong") == "qwen3:8b" and model_name(settings, "fast") == "qwen3:4b"


def test_azure_requests_are_correct_and_cost_is_recorded(tmp_path):
    captured = []

    async def completions(request):
        captured.append((request.path, request.headers.get("api-key"), await request.json()))
        return web.json_response({
            "id": "1", "object": "chat.completion", "created": 1, "model": "gpt-5",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000, "total_tokens": 1_100_000},
        })

    async def run():
        app = web.Application()
        app.router.add_post("/{tail:.*}", completions)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        settings = _azure(endpoint=f"http://127.0.0.1:{port}/", audit_path=tmp_path / "audit.jsonl")
        audit = AuditLog(settings.audit_path)
        try:
            answer = await LLM(settings, audit).ask("test", "You write tests.", "Say hello")
        finally:
            await runner.cleanup()
        return answer, audit.summary()

    answer, summary = asyncio.run(run())
    assert answer == "hello"
    path, key, body = captured[0]
    assert key == "test-key" and "chat/completions" in path
    assert body["model"] == "gpt-5" or "deployments/gpt-5/" in path
    assert body["max_completion_tokens"] == 4000 and "max_tokens" not in body  # GPT-5 rejects max_tokens
    assert "temperature" not in body  # GPT-5 rejects custom temperatures
    assert body["reasoning_effort"] == "low"  # reasoning shares the output cap; unlimited, it crowds out the answer
    # 1M input at $1.375 + 0.1M output at $11
    assert round(summary["cost_usd"], 3) == 2.475 == round(summary["estimated_azure_cost_usd"], 3)
