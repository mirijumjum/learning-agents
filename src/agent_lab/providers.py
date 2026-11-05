"""Choosing which LLM backs the agents. This is the ONLY provider-specific code."""

from __future__ import annotations

import os

from agent_lab.mock_client import MockChatClient
from agent_lab.workflow import ClientFactory

PROVIDERS = {
    "mock": "Offline scripted client (no API key). Knows: palindrome, fizzbuzz, celsius.",
    "openai": "OpenAI or any OpenAI-compatible server. Env: OPENAI_API_KEY, OPENAI_MODEL (opt. OPENAI_BASE_URL).",
    "azure": "Azure OpenAI. Env: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_MODEL (deployment name).",
    "anthropic": "Anthropic (pip install 'agent-lab[anthropic]'). Env: ANTHROPIC_API_KEY, ANTHROPIC_CHAT_MODEL.",
}


class ProviderConfigError(RuntimeError):
    pass


def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ProviderConfigError(f"Missing environment variable(s): {', '.join(missing)}")


def make_client_factory(provider: str, model: str | None = None) -> ClientFactory:
    """Return a function role -> chat client. Real providers share one client across all agents."""
    if provider == "mock":
        return lambda role: MockChatClient(role)

    if provider == "openai":
        from agent_framework.openai import OpenAIChatCompletionClient

        _require_env("OPENAI_API_KEY")
        if not (model or os.environ.get("OPENAI_MODEL") or os.environ.get("OPENAI_CHAT_COMPLETION_MODEL")):
            raise ProviderConfigError("Set OPENAI_MODEL or pass --model.")
        client = OpenAIChatCompletionClient(model=model, api_key=os.environ["OPENAI_API_KEY"])

    elif provider == "azure":
        from agent_framework.openai import OpenAIChatCompletionClient

        _require_env("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY")
        if not (model or os.environ.get("AZURE_OPENAI_MODEL") or os.environ.get("AZURE_OPENAI_CHAT_COMPLETION_MODEL")):
            raise ProviderConfigError("Set AZURE_OPENAI_MODEL (your deployment name) or pass --model.")
        client = OpenAIChatCompletionClient(
            model=model,
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
        )

    elif provider == "anthropic":
        try:
            from agent_framework.anthropic import AnthropicClient
        except ImportError as exc:
            raise ProviderConfigError("Install the extra: pip install -e '.[anthropic]'") from exc

        _require_env("ANTHROPIC_API_KEY")
        if not (model or os.environ.get("ANTHROPIC_CHAT_MODEL")):
            raise ProviderConfigError("Set ANTHROPIC_CHAT_MODEL or pass --model.")
        client = AnthropicClient(model=model)

    else:
        raise ProviderConfigError(f"Unknown provider {provider!r}. Choose one of: {', '.join(PROVIDERS)}")

    return lambda role: client
