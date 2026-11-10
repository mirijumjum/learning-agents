"""Proves DevAssist can call the OpenAI and Anthropic APIs directly, against local stand-ins for both.

No real API is called: each test starts a small HTTP server on 127.0.0.1 that records the request
and answers the way the vendor's API does.
"""

import asyncio
import json

import pytest
from aiohttp import web

from devassist.audit import AuditLog, VENDORS, vendor_estimates
from devassist.config import ConfigError, Settings
from devassist.llm import LLM, TIER_BY_ROLE, model_name, reasoning_effort


def _openai(**overrides):
    return Settings(profile="openai", openai_api_key="sk-test", **overrides)


def _anthropic(**overrides):
    return Settings(profile="anthropic", anthropic_api_key="sk-ant-test", **overrides)


async def _serve(handler):
    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def _ask(settings, role, deliberate=False):
    async def run():
        audit = AuditLog(settings.audit_path)
        answer = await LLM(settings, audit).ask(role, "You help developers.", "Say hello", deliberate=deliberate)
        return answer, audit.summary()

    return asyncio.run(run())


# --- model selection and configuration ---------------------------------------------


def test_each_agent_gets_the_vendor_model_for_its_tier():
    openai, anthropic = _openai(), _anthropic()
    assert {role: model_name(openai, TIER_BY_ROLE[role]) for role in ("test", "docs", "git", "router")} == {
        "test": "gpt-6.1-sol", "docs": "gpt-6.1-sol", "git": "gpt-6-luna", "router": "gpt-6-luna",
    }
    assert {role: model_name(anthropic, TIER_BY_ROLE[role]) for role in ("test", "trace_complex", "trace")} == {
        "test": "claude-opus-5-5", "trace_complex": "claude-opus-5-5", "trace": "claude-haiku-5-5",
    }


def test_models_and_keys_come_from_the_environment():
    settings = Settings.from_env({
        "DEVASSIST_PROFILE": "anthropic", "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_MODEL_STRONG": "claude-sonnet-5-5", "ANTHROPIC_MODEL_FAST": "claude-haiku-5-5",
    })
    assert model_name(settings, "strong") == "claude-sonnet-5-5"
    settings = Settings.from_env({
        "DEVASSIST_PROFILE": "openai", "OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "https://eu.api.openai.com/v1/",
        "OPENAI_MODEL_STRONG": "gpt-5", "OPENAI_MODEL_FAST": "gpt-5-mini",
    })
    assert (model_name(settings, "strong"), model_name(settings, "fast")) == ("gpt-5", "gpt-5-mini")
    assert settings.openai_base_url == "https://eu.api.openai.com/v1"


@pytest.mark.parametrize("profile, variable", [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")])
def test_a_paid_profile_without_a_key_is_refused(profile, variable):
    with pytest.raises(ConfigError, match=variable):
        Settings.from_env({"DEVASSIST_PROFILE": profile})


def test_anthropic_effort_never_asks_for_no_thinking():
    # Current Claude models reject disabled thinking, so "none" becomes the cheapest valid level.
    assert reasoning_effort(_anthropic(reasoning_effort="none")) == "low"
    assert reasoning_effort(_anthropic()) == "low"
    assert reasoning_effort(_anthropic(reasoning_effort="high")) == "high"
    assert reasoning_effort(_openai()) == "low"


# --- OpenAI API ---------------------------------------------------------------------


def test_openai_requests_are_correct_and_cost_is_recorded(tmp_path):
    captured = []

    async def completions(request):
        captured.append((request.path, request.headers.copy(), await request.json()))
        return web.json_response({
            "id": "1", "object": "chat.completion", "created": 1, "model": "gpt-6.1-sol",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}],
            "usage": {
                "prompt_tokens": 1_000_000, "completion_tokens": 100_000, "total_tokens": 1_100_000,
                "prompt_tokens_details": {"cached_tokens": 200_000},
            },
        })

    async def run():
        runner, port = await _serve(completions)
        settings = _openai(openai_base_url=f"http://127.0.0.1:{port}/v1", audit_path=tmp_path / "audit.jsonl")
        try:
            audit = AuditLog(settings.audit_path)
            answer = await LLM(settings, audit).ask("test", "You write tests.", "Say hello")
        finally:
            await runner.cleanup()
        return answer, audit.summary()

    answer, summary = asyncio.run(run())
    assert answer == "hello"
    path, headers, body = captured[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert body["model"] == "gpt-6.1-sol"
    assert body["max_completion_tokens"] == 4000 and "max_tokens" not in body  # reasoning models reject max_tokens
    assert body["reasoning_effort"] == "low"
    assert "temperature" not in body
    # 800K uncached input at $2 + 200K cached at $0.10 + 100K output at $10
    assert round(summary["cost_usd"], 4) == 2.62
    assert round(summary["cost_usd_by_profile"]["openai"], 4) == 2.62


def test_openai_fast_tier_uses_the_small_model(tmp_path):
    captured = []

    async def completions(request):
        captured.append(await request.json())
        return web.json_response({
            "id": "1", "object": "chat.completion", "created": 1, "model": "gpt-6-luna",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "total_tokens": 2_000_000},
        })

    async def run():
        runner, port = await _serve(completions)
        settings = _openai(openai_base_url=f"http://127.0.0.1:{port}/v1", audit_path=tmp_path / "audit.jsonl")
        try:
            audit = AuditLog(settings.audit_path)
            await LLM(settings, audit).ask("git", "You use GitLab.", "list MRs")
        finally:
            await runner.cleanup()
        return audit.summary()

    summary = asyncio.run(run())
    assert captured[0]["model"] == "gpt-6-luna"
    assert round(summary["cost_usd"], 4) == 0.60  # 1M in at $0.10 + 1M out at $0.50


# --- Anthropic API ------------------------------------------------------------------


def _anthropic_message(model, usage):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn", "stop_sequence": None, "usage": usage,
    }


def test_anthropic_requests_are_correct_and_cost_is_recorded(tmp_path):
    captured = []

    async def messages(request):
        captured.append((request.path, request.headers.copy(), await request.json()))
        return web.json_response(_anthropic_message("claude-opus-5-5", {
            "input_tokens": 800_000, "output_tokens": 100_000,
            "cache_read_input_tokens": 200_000, "cache_creation_input_tokens": 0,
        }))

    async def run():
        runner, port = await _serve(messages)
        settings = _anthropic(anthropic_base_url=f"http://127.0.0.1:{port}", audit_path=tmp_path / "audit.jsonl")
        try:
            audit = AuditLog(settings.audit_path)
            answer = await LLM(settings, audit).ask("test", "You write tests.", "Say hello")
        finally:
            await runner.cleanup()
        return answer, audit.summary()

    answer, summary = asyncio.run(run())
    assert answer == "hello"
    path, headers, body = captured[0]
    assert path == "/v1/messages"
    assert headers["x-api-key"] == "sk-ant-test" and "Authorization" not in headers
    assert headers.get("anthropic-version")
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 4000 and "max_completion_tokens" not in body
    assert body["output_config"] == {"effort": "low"}  # effort, not reasoning_effort
    assert "reasoning_effort" not in body and "temperature" not in body
    assert body.get("thinking", {"type": "adaptive"}).get("type") == "adaptive"  # disabling thinking is a 400
    assert body["system"] and body["messages"][0]["role"] == "user"
    # 800K uncached input at $4 + 200K cache reads at $0.20 + 100K output at $20
    assert round(summary["cost_usd"], 4) == 5.24
    assert round(summary["cost_usd_by_profile"]["anthropic"], 4) == 5.24


def test_anthropic_deliberate_call_raises_effort_and_cap(tmp_path):
    captured = []

    async def messages(request):
        captured.append(await request.json())
        return web.json_response(_anthropic_message("claude-haiku-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}))

    async def run():
        runner, port = await _serve(messages)
        settings = _anthropic(anthropic_base_url=f"http://127.0.0.1:{port}", audit_path=tmp_path / "audit.jsonl")
        try:
            audit = AuditLog(settings.audit_path)
            await LLM(settings, audit).ask("trace", "You read logs.", "why?", deliberate=True)
        finally:
            await runner.cleanup()
        return audit.summary()

    summary = asyncio.run(run())
    body = captured[0]
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 12000 and body["output_config"] == {"effort": "medium"}
    assert round(summary["cost_usd"], 4) == 0.60  # 1M in at $0.10 + 1M out at $0.50


# --- usage: every vendor estimated from any run ---------------------------------------


def test_usage_estimates_every_vendor_even_on_the_free_profile(tmp_path):
    settings = Settings(profile="mock", audit_path=tmp_path / "audit.jsonl")
    answer, summary = _ask(settings, "router")
    assert summary["cost_usd"] == 0.0
    estimates = summary["estimated_cost_usd_by_vendor"]
    assert set(estimates) == set(VENDORS) == {"azure", "openai", "anthropic"}
    # the mock reports 100 input and 50 output tokens per call
    assert estimates == pytest.approx(vendor_estimates("fast", 100, 50))
    assert estimates["azure"] == pytest.approx(summary["estimated_azure_cost_usd"])


def test_guide_volume_per_vendor():
    """The design guide's 20-developer month: 40M in / 7M out on the strong tier, 13M / 2M on the fast one."""
    strong = vendor_estimates("strong", 40_000_000, 7_000_000)
    fast = vendor_estimates("fast", 13_000_000, 2_000_000)
    total = {vendor: round(strong[vendor] + fast[vendor], 2) for vendor in VENDORS}
    assert total == pytest.approx({"azure": 139.98, "openai": 152.3, "anthropic": 302.3}, abs=0.01)
    json.dumps(total)


# --- tool calls: the Git agent needs them on both vendors ----------------------------


def _pipeline_tool(calls):
    from typing import Annotated

    from agent_framework import tool
    from pydantic import Field

    async def pipeline_status(pipeline_id: Annotated[int, Field(description="Pipeline number")]) -> str:
        """Get the status of one pipeline."""
        calls.append(pipeline_id)
        return json.dumps({"id": pipeline_id, "status": "failed"})

    return tool(pipeline_status, name="pipeline_status")


def test_openai_tool_call_round_trip(tmp_path):
    bodies, calls = [], []

    async def completions(request):
        body = await request.json()
        bodies.append(body)
        if len(bodies) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "pipeline_status", "arguments": '{"pipeline_id": 5512}'}},
            ]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": "Pipeline #5512 failed."}, "stop"
        return web.json_response({
            "id": str(len(bodies)), "object": "chat.completion", "created": 1, "model": "gpt-6-luna",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    async def run():
        runner, port = await _serve(completions)
        settings = _openai(openai_base_url=f"http://127.0.0.1:{port}/v1", audit_path=tmp_path / "audit.jsonl")
        try:
            return await LLM(settings, AuditLog(settings.audit_path)).ask("git", "You use GitLab.", "Is #5512 red?", tools=[_pipeline_tool(calls)])
        finally:
            await runner.cleanup()

    answer = asyncio.run(run())
    assert answer == "Pipeline #5512 failed." and calls == [5512]
    assert bodies[0]["tools"][0]["function"]["name"] == "pipeline_status"
    tool_message = bodies[1]["messages"][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_1"


def test_anthropic_tool_call_round_trip_ignores_thinking_blocks(tmp_path):
    bodies, calls = [], []

    async def messages(request):
        body = await request.json()
        bodies.append(body)
        thinking = {"type": "thinking", "thinking": "", "signature": "sig"}  # current models think; text is omitted by default
        if len(bodies) == 1:
            content = [thinking, {"type": "tool_use", "id": "toolu_1", "name": "pipeline_status", "input": {"pipeline_id": 5512}}]
            stop = "tool_use"
        else:
            content, stop = [thinking, {"type": "text", "text": "Pipeline #5512 failed."}], "end_turn"
        return web.json_response({
            "id": f"msg_{len(bodies)}", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    async def run():
        runner, port = await _serve(messages)
        settings = _anthropic(anthropic_base_url=f"http://127.0.0.1:{port}", audit_path=tmp_path / "audit.jsonl")
        try:
            return await LLM(settings, AuditLog(settings.audit_path)).ask("git", "You use GitLab.", "Is #5512 red?", tools=[_pipeline_tool(calls)])
        finally:
            await runner.cleanup()

    answer = asyncio.run(run())
    assert answer == "Pipeline #5512 failed." and calls == [5512]
    first, second = bodies
    assert first["tools"][0]["name"] == "pipeline_status" and "input_schema" in first["tools"][0]
    assert first.get("tool_choice", {"type": "auto"})["type"] == "auto"  # forced tool choice is a 400 on current models
    results = [block for block in second["messages"][-1]["content"] if block.get("type") == "tool_result"]
    assert results and results[0]["tool_use_id"] == "toolu_1"
    # the thinking block goes back unchanged with the tool call, as the API requires
    assert any(block.get("type") == "thinking" for block in second["messages"][-2]["content"])
