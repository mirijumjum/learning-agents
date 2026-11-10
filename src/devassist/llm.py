"""The only place that knows which model backs each agent.

    role     tier     ollama (free)   azure        openai        anthropic
    router   fast     qwen3:4b        gpt-5-mini   gpt-6-luna    claude-haiku-5-5
    git      fast     qwen3:4b        gpt-5-mini   gpt-6-luna    claude-haiku-5-5
    trace    fast     qwen3:4b        gpt-5-mini   gpt-6-luna    claude-haiku-5-5
    test     strong   qwen3:8b        gpt-5        gpt-6.1-sol   claude-opus-5-5
    docs     strong   qwen3:8b        gpt-5        gpt-6.1-sol   claude-opus-5-5
    trace_complex  strong (incidents with many distinct errors)

openai and anthropic call each vendor's API directly, with no Azure services.
Every model name can be changed with environment variables.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any

from agent_framework import Agent

from devassist.audit import AuditLog, price_for, priced_cost, token_cost, vendor_estimates
from devassist.config import PAID_PROFILES, ConfigError, Settings

TIER_BY_ROLE = {"router": "fast", "git": "fast", "trace": "fast", "trace_complex": "strong", "test": "strong", "docs": "strong"}
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)
DELIBERATE_EFFORT = "medium"
# Anthropic's current models always think; effort is the only dial and has no "none".
_ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")


class LLMTimeout(RuntimeError):
    """A model call ran too long. Local models can reason for minutes without stopping."""


def model_name(settings: Settings, tier: str) -> str:
    if settings.profile == "ollama":
        return settings.ollama_model_strong if tier == "strong" else settings.ollama_model_fast
    if settings.profile == "azure":
        return settings.azure_deployment_strong if tier == "strong" else settings.azure_deployment_fast
    if settings.profile == "openai":
        return settings.openai_model_strong if tier == "strong" else settings.openai_model_fast
    if settings.profile == "anthropic":
        return settings.anthropic_model_strong if tier == "strong" else settings.anthropic_model_fast
    return "mock"


def reasoning_effort(settings: Settings) -> str:
    """Reasoning tokens count against max_tokens. Left unlimited, they crowd out the actual answer."""
    if settings.profile == "mock":
        return ""
    effort = settings.reasoning_effort or ("none" if settings.profile == "ollama" else "low")
    if settings.profile == "anthropic" and effort not in _ANTHROPIC_EFFORTS:
        return "low"  # "none"/"minimal" would be rejected; low is the cheapest valid level
    return effort


def effort_options(settings: Settings, effort: str) -> dict[str, Any]:
    """Each vendor names the reasoning dial differently."""
    if not effort:
        return {}
    if settings.profile == "anthropic":
        # Messages API: output_config.effort. Thinking stays adaptive; disabling it is a 400 on current models.
        return {"output_config": {"effort": effort if effort in _ANTHROPIC_EFFORTS else "low"}}
    return {"reasoning_effort": effort}


def _make_client(settings: Settings, role: str, tier: str) -> Any:
    if settings.profile == "mock":
        from devassist.mock_llm import MockDevClient

        return MockDevClient(role, flaky_tests=settings.mock_flaky_tests)

    from agent_framework.openai import OpenAIChatCompletionClient

    model = model_name(settings, tier)
    if settings.profile == "anthropic":
        return _anthropic_client(settings, model)
    if settings.profile == "ollama":
        return OpenAIChatCompletionClient(model=model, api_key="ollama", base_url=settings.ollama_base_url)
    if settings.profile == "openai":
        # Direct OpenAI API. base_url only for an EU data-residency project or a test stand-in.
        return OpenAIChatCompletionClient(model=model, api_key=settings.openai_api_key, base_url=settings.openai_base_url or None)

    kwargs: dict[str, Any] = {"model": model, "azure_endpoint": settings.azure_endpoint}
    if settings.azure_api_version:
        kwargs["api_version"] = settings.azure_api_version
    if settings.azure_api_key:
        kwargs["api_key"] = settings.azure_api_key
    else:
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise ConfigError("Managed identity needs: pip install -e '.[azure]'") from exc
        kwargs["credential"] = DefaultAzureCredential()
    return OpenAIChatCompletionClient(**kwargs)


def _anthropic_client(settings: Settings, model: str) -> Any:
    try:
        from agent_framework.anthropic import AnthropicClient
    except ImportError as exc:
        raise ConfigError("DEVASSIST_PROFILE=anthropic needs: pip install -e '.[anthropic]'") from exc
    return AnthropicClient(model=model, api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url or None)


def _uncached_input(input_tokens: int, cache_read: int, cache_write: int) -> int:
    """Both connectors report input_token_count including cached tokens; the uncached part bills at the full rate."""
    return max(0, input_tokens - cache_read - cache_write)


class LLM:
    """Runs agents and records what each call consumed."""

    def __init__(self, settings: Settings, audit: AuditLog) -> None:
        self.settings = settings
        self.audit = audit
        self._clients: dict[str, Any] = {}

    def _client(self, role: str) -> Any:
        if role not in self._clients:
            self._clients[role] = _make_client(self.settings, role, TIER_BY_ROLE[role])
        return self._clients[role]

    async def ask(self, role: str, instructions: str, prompt: str, tools: Sequence[Any] | None = None, deliberate: bool = False) -> str:
        """deliberate: for rare judgement calls. More reasoning, with a larger cap so it can't crowd out the answer."""
        tier = TIER_BY_ROLE[role]
        agent = Agent(self._client(role), instructions, name=role, tools=list(tools or []))
        options: dict[str, Any] = {"max_tokens": self.settings.max_output_tokens * (3 if deliberate else 1)}
        effort = DELIBERATE_EFFORT if deliberate and self.settings.profile != "mock" else reasoning_effort(self.settings)
        options.update(effort_options(self.settings, effort))
        try:
            response = await asyncio.wait_for(agent.run(prompt[: self.settings.max_input_chars], options=options), timeout=self.settings.llm_timeout_seconds)
        except asyncio.TimeoutError:
            self.audit.event("llm_timeout", role=role, tier=tier, seconds=self.settings.llm_timeout_seconds)
            raise LLMTimeout(f"The {role} model took longer than {self.settings.llm_timeout_seconds:.0f}s, so I stopped it. Please try again, or narrow the request.") from None

        usage = response.usage_details or {}
        input_tokens = int(usage.get("input_token_count") or 0)
        output_tokens = int(usage.get("output_token_count") or 0)
        cache_read = int(usage.get("cache_read_input_token_count") or 0)
        cache_write = int(usage.get("cache_creation_input_token_count") or 0)
        uncached = _uncached_input(input_tokens, cache_read, cache_write)
        model = model_name(self.settings, tier)
        estimated = token_cost(tier, input_tokens, output_tokens)
        cost = 0.0
        if self.settings.profile in PAID_PROFILES:
            cost = priced_cost(price_for(self.settings.profile, tier, model), uncached, output_tokens, cache_read, cache_write)
        self.audit.event(
            "llm", role=role, tier=tier, model=model, profile=self.settings.profile,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            cost_usd=round(cost, 6),
            estimated_azure_cost_usd=estimated,
            estimated_cost_usd_by_vendor=vendor_estimates(tier, input_tokens, output_tokens),
        )
        return _THINK.sub("", response.text or "").strip()
