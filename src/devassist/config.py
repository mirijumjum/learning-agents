"""All configuration comes from environment variables.

Moving from the free local setup to the paid one means changing these values,
never the code: DEVASSIST_PROFILE=ollama becomes DEVASSIST_PROFILE=azure, or
DEVASSIST_PROFILE=openai-compatible for a self-hosted model server such as vLLM.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PROFILES = ("mock", "ollama", "azure", "openai-compatible")
REASONING_MODES = ("effort", "thinking-flag", "omit")


class ConfigError(RuntimeError):
    pass


def load_dotenv_file() -> None:
    """Load .env.devassist if present. Real environment variables always win."""
    from dotenv import load_dotenv

    load_dotenv(".env.devassist", override=False)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    profile: str = "mock"

    # Local models. "strong" stands in for GPT-5, "fast" for GPT-5 mini.
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model_strong: str = "qwen3:8b"
    ollama_model_fast: str = "qwen3:4b"

    # Azure OpenAI. With no API key, the managed identity is used instead.
    azure_endpoint: str = ""
    azure_api_key: str = ""
    azure_deployment_strong: str = "gpt-5"
    azure_deployment_fast: str = "gpt-5-mini"
    azure_api_version: str = ""

    # Any OpenAI-compatible server: vLLM, Ollama's /v1, llama.cpp server, LiteLLM...
    # reasoning mode, how DEVASSIST_REASONING_EFFORT reaches the server:
    #   effort         reasoning_effort field (gpt-oss on vLLM, Ollama)
    #   thinking-flag  chat_template_kwargs.enable_thinking (Qwen3 on vLLM: "none" turns thinking off)
    #   omit           send nothing; the server's defaults apply
    openai_compat_base_url: str = ""
    openai_compat_base_url_fast: str = ""  # only when the fast model runs on another server; empty: same server
    openai_compat_api_key: str = ""
    openai_compat_model_strong: str = ""
    openai_compat_model_fast: str = ""
    openai_compat_reasoning_mode: str = "effort"

    # GitLab
    gitlab_url: str = "http://127.0.0.1:8929"
    gitlab_token: str = "local-dev-token"
    gitlab_projects: tuple[str, ...] = ()  # allow-list of full paths; empty means whatever the token can see
    gitlab_webhook_secret: str = ""
    default_project: str = ""
    user_map: Mapping[str, str] = field(default_factory=dict)  # Teams e-mail -> GitLab username

    # Log store: jsonl | azure | elasticsearch
    log_store: str = "jsonl"
    log_jsonl_path: Path = Path(".devassist/logs.jsonl")
    log_analytics_workspace_id: str = ""
    elastic_url: str = ""
    elastic_api_key: str = ""
    elastic_index: str = "logs-*"
    timezone: str = "UTC"

    # Cost control
    max_diff_chars: int = 60_000
    max_input_chars: int = 48_000
    max_output_tokens: int = 4_000
    reasoning_effort: str = ""  # empty: "none" on Ollama and openai-compatible, "low" on Azure
    llm_timeout_seconds: float = 180.0
    max_test_attempts: int = 3
    pipeline_timeout_seconds: float = 900.0
    pipeline_poll_seconds: float = 3.0

    # Audit, access
    audit_path: Path = Path(".devassist/audit.jsonl")
    allowed_tenant_id: str = ""
    appinsights_connection_string: str = ""

    # GitLab webhook automations. All off until someone opts in.
    auto_tests_on_mr: bool = False
    auto_changelog_on_merge: bool = False
    auto_trace_on_pipeline_failure: bool = False
    teams_notify_url: str = ""

    # Mock only: make the first generated test wrong, to exercise the retry loop.
    mock_flaky_tests: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        defaults = cls()

        def text(name: str, default: str) -> str:
            return e.get(name) or default

        def integer(name: str, default: int) -> int:
            raw = e.get(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc

        try:
            user_map = json.loads(e.get("DEVASSIST_USER_MAP") or "{}")
        except json.JSONDecodeError as exc:
            raise ConfigError(f"DEVASSIST_USER_MAP is not valid JSON: {exc}") from exc

        settings = cls(
            profile=text("DEVASSIST_PROFILE", defaults.profile),
            ollama_base_url=text("OLLAMA_BASE_URL", defaults.ollama_base_url),
            ollama_model_strong=text("OLLAMA_MODEL_STRONG", defaults.ollama_model_strong),
            ollama_model_fast=text("OLLAMA_MODEL_FAST", defaults.ollama_model_fast),
            azure_endpoint=text("AZURE_OPENAI_ENDPOINT", ""),
            azure_api_key=text("AZURE_OPENAI_API_KEY", ""),
            azure_deployment_strong=text("AZURE_OPENAI_DEPLOYMENT_STRONG", defaults.azure_deployment_strong),
            azure_deployment_fast=text("AZURE_OPENAI_DEPLOYMENT_FAST", defaults.azure_deployment_fast),
            azure_api_version=text("AZURE_OPENAI_API_VERSION", ""),
            openai_compat_base_url=text("OPENAI_COMPATIBLE_BASE_URL", "").rstrip("/"),
            openai_compat_base_url_fast=text("OPENAI_COMPATIBLE_BASE_URL_FAST", "").rstrip("/"),
            openai_compat_api_key=text("OPENAI_COMPATIBLE_API_KEY", ""),
            openai_compat_model_strong=text("OPENAI_COMPATIBLE_MODEL_STRONG", ""),
            openai_compat_model_fast=text("OPENAI_COMPATIBLE_MODEL_FAST", ""),
            openai_compat_reasoning_mode=text("OPENAI_COMPATIBLE_REASONING_MODE", defaults.openai_compat_reasoning_mode),
            gitlab_url=text("GITLAB_URL", defaults.gitlab_url).rstrip("/"),
            gitlab_token=text("GITLAB_TOKEN", defaults.gitlab_token),
            gitlab_projects=_csv(e.get("GITLAB_PROJECTS")),
            gitlab_webhook_secret=text("GITLAB_WEBHOOK_SECRET", ""),
            default_project=text("DEVASSIST_DEFAULT_PROJECT", ""),
            user_map=user_map,
            log_store=text("DEVASSIST_LOG_STORE", defaults.log_store),
            log_jsonl_path=Path(text("DEVASSIST_LOG_PATH", str(defaults.log_jsonl_path))),
            log_analytics_workspace_id=text("LOG_ANALYTICS_WORKSPACE_ID", ""),
            elastic_url=text("ELASTIC_URL", "").rstrip("/"),
            elastic_api_key=text("ELASTIC_API_KEY", ""),
            elastic_index=text("ELASTIC_INDEX", defaults.elastic_index),
            timezone=text("DEVASSIST_TIMEZONE", defaults.timezone),
            max_diff_chars=integer("DEVASSIST_MAX_DIFF_CHARS", defaults.max_diff_chars),
            max_input_chars=integer("DEVASSIST_MAX_INPUT_CHARS", defaults.max_input_chars),
            max_output_tokens=integer("DEVASSIST_MAX_OUTPUT_TOKENS", defaults.max_output_tokens),
            reasoning_effort=text("DEVASSIST_REASONING_EFFORT", ""),
            llm_timeout_seconds=integer("DEVASSIST_LLM_TIMEOUT", int(defaults.llm_timeout_seconds)),
            max_test_attempts=integer("DEVASSIST_MAX_TEST_ATTEMPTS", defaults.max_test_attempts),
            pipeline_timeout_seconds=integer("DEVASSIST_PIPELINE_TIMEOUT", int(defaults.pipeline_timeout_seconds)),
            audit_path=Path(text("DEVASSIST_AUDIT_PATH", str(defaults.audit_path))),
            allowed_tenant_id=text("DEVASSIST_ALLOWED_TENANT_ID", ""),
            appinsights_connection_string=text("APPLICATIONINSIGHTS_CONNECTION_STRING", ""),
            auto_tests_on_mr=_flag(e.get("DEVASSIST_AUTO_TESTS_ON_MR")),
            auto_changelog_on_merge=_flag(e.get("DEVASSIST_AUTO_CHANGELOG_ON_MERGE")),
            auto_trace_on_pipeline_failure=_flag(e.get("DEVASSIST_AUTO_TRACE_ON_PIPELINE_FAILURE")),
            teams_notify_url=text("TEAMS_NOTIFY_WEBHOOK_URL", ""),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.profile not in PROFILES:
            raise ConfigError(f"DEVASSIST_PROFILE must be one of {', '.join(PROFILES)}, got {self.profile!r}")
        if self.profile == "azure" and not self.azure_endpoint:
            raise ConfigError("DEVASSIST_PROFILE=azure needs AZURE_OPENAI_ENDPOINT.")
        if self.profile == "openai-compatible":
            missing = [name for name, value in (
                ("OPENAI_COMPATIBLE_BASE_URL", self.openai_compat_base_url),
                ("OPENAI_COMPATIBLE_MODEL_STRONG", self.openai_compat_model_strong),
                ("OPENAI_COMPATIBLE_MODEL_FAST", self.openai_compat_model_fast),
            ) if not value]
            if missing:
                raise ConfigError(f"DEVASSIST_PROFILE=openai-compatible needs {', '.join(missing)}.")
            if self.openai_compat_reasoning_mode not in REASONING_MODES:
                raise ConfigError(f"OPENAI_COMPATIBLE_REASONING_MODE must be one of {', '.join(REASONING_MODES)}, got {self.openai_compat_reasoning_mode!r}")
        if self.log_store not in ("jsonl", "azure", "elasticsearch"):
            raise ConfigError(f"DEVASSIST_LOG_STORE must be jsonl, azure or elasticsearch, got {self.log_store!r}")
        if self.max_test_attempts < 1:
            raise ConfigError("DEVASSIST_MAX_TEST_ATTEMPTS must be at least 1.")
