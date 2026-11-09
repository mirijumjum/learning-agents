"""The only place that knows which model backs each agent.

    role     tier     free profile (ollama)   paid profile (azure)
    router   fast     qwen3:4b                gpt-5-mini
    git      fast     qwen3:4b                gpt-5-mini
    trace    fast     qwen3:4b                gpt-5-mini
    test     strong   qwen3:8b                gpt-5
    docs     strong   qwen3:8b                gpt-5
    trace_complex  strong (incidents with many distinct errors)
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any

from agent_framework import Agent

from devassist.audit import AuditLog, token_cost
from devassist.config import ConfigError, Settings

TIER_BY_ROLE = {"router": "fast", "git": "fast", "trace": "fast", "trace_complex": "strong", "test": "strong", "docs": "strong"}
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)
DELIBERATE_EFFORT = "medium"


class LLMTimeout(RuntimeError):
    """A model call ran too long. Local models can reason for minutes without stopping."""


def model_name(settings: Settings, tier: str) -> str:
    if settings.profile == "ollama":
        return settings.ollama_model_strong if tier == "strong" else settings.ollama_model_fast
    if settings.profile == "azure":
        return settings.azure_deployment_strong if tier == "strong" else settings.azure_deployment_fast
    return "mock"


def reasoning_effort(settings: Settings) -> str:
    """Reasoning tokens count against max_tokens. Left unlimited, they crowd out the actual answer."""
    if settings.profile == "mock":
        return ""
    return settings.reasoning_effort or ("none" if settings.profile == "ollama" else "low")


def _make_client(settings: Settings, role: str, tier: str) -> Any:
    if settings.profile == "mock":
        from devassist.mock_llm import MockDevClient

        return MockDevClient(role, flaky_tests=settings.mock_flaky_tests)

    from agent_framework.openai import OpenAIChatCompletionClient

    model = model_name(settings, tier)
    if settings.profile == "ollama":
        return OpenAIChatCompletionClient(model=model, api_key="ollama", base_url=settings.ollama_base_url)

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
        if effort:
            options["reasoning_effort"] = effort
        try:
            response = await asyncio.wait_for(agent.run(prompt[: self.settings.max_input_chars], options=options), timeout=self.settings.llm_timeout_seconds)
        except asyncio.TimeoutError:
            self.audit.event("llm_timeout", role=role, tier=tier, seconds=self.settings.llm_timeout_seconds)
            raise LLMTimeout(f"The {role} model took longer than {self.settings.llm_timeout_seconds:.0f}s, so I stopped it. Please try again, or narrow the request.") from None

        usage = response.usage_details or {}
        input_tokens = int(usage.get("input_token_count") or 0)
        output_tokens = int(usage.get("output_token_count") or 0)
        estimated = token_cost(tier, input_tokens, output_tokens)
        self.audit.event(
            "llm", role=role, tier=tier, model=model_name(self.settings, tier), profile=self.settings.profile,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_usd=estimated if self.settings.profile == "azure" else 0.0,
            estimated_azure_cost_usd=estimated,
        )
        return _THINK.sub("", response.text or "").strip()
