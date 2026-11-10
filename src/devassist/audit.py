"""Audit trail: every request, tool call, token count and cost, as JSON lines.

The same events go to the Python logger, so once Application Insights is
configured they reach it with no further changes.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("devassist.audit")

# USD per 1M tokens (input, output): Data Zone Standard EU list prices from the design guide.
PRICES = {"strong": (1.375, 11.00), "fast": (0.275, 2.20)}


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens. cache_read and cache_write default to the input price."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None


# Public list prices, checked 2026-11-10. Sources:
#   Azure:     the design guide (GPT-5 / GPT-5 mini, Data Zone Standard EU)
#   OpenAI:    https://developers.openai.com/api/docs/pricing (Standard tier, short context)
#   Anthropic: https://platform.claude.com/docs/en/about-claude/pricing (global routing, 5-minute cache writes)
MODEL_PRICES: dict[str, Price] = {
    # OpenAI
    "gpt-6-astra": Price(10.00, 50.00, cache_read=1.00),
    "gpt-6.1-sol": Price(2.00, 10.00, cache_read=0.10),
    "gpt-6.1-terra": Price(1.00, 5.00, cache_read=0.05),
    "gpt-6-luna": Price(0.10, 0.50, cache_read=0.01),
    "gpt-5.4": Price(2.50, 15.00, cache_read=0.25),
    "gpt-5.4-mini": Price(0.75, 4.50, cache_read=0.075),
    "gpt-5": Price(1.25, 10.00, cache_read=0.125),
    "gpt-5-mini": Price(0.25, 2.00, cache_read=0.025),
    # Anthropic
    "claude-opus-5-5": Price(4.00, 20.00, cache_read=0.20, cache_write=5.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, cache_read=0.10, cache_write=2.50),
    "claude-haiku-5-5": Price(0.10, 0.50, cache_read=0.01, cache_write=0.125),  # prompts up to 100K tokens
}

# What each vendor's default strong/fast pair costs. Used to estimate every vendor from any run,
# including free local ones, so the pilot can be priced before paying anyone.
VENDOR_TIER_PRICES: dict[str, dict[str, Price]] = {
    "azure": {"strong": Price(*PRICES["strong"]), "fast": Price(*PRICES["fast"])},
    "openai": {"strong": MODEL_PRICES["gpt-6.1-sol"], "fast": MODEL_PRICES["gpt-6-luna"]},
    "anthropic": {"strong": MODEL_PRICES["claude-opus-5-5"], "fast": MODEL_PRICES["claude-haiku-5-5"]},
}
VENDORS = tuple(VENDOR_TIER_PRICES)


@dataclass
class RequestContext:
    user: str = "local"
    email: str = ""
    conversation_id: str = "local"
    source: str = "cli"
    request_id: str = ""

    def __post_init__(self) -> None:
        self.request_id = self.request_id or uuid.uuid4().hex[:12]


current_request: ContextVar[RequestContext | None] = ContextVar("devassist_request", default=None)


def token_cost(tier: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = PRICES[tier]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def price_for(vendor: str, tier: str, model: str = "") -> Price:
    """The model's own price when known, else the vendor's default for the tier."""
    if vendor != "azure" and model in MODEL_PRICES:
        return MODEL_PRICES[model]
    return VENDOR_TIER_PRICES[vendor][tier]


def priced_cost(price: Price, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    """input_tokens are the uncached ones; cached reads and writes are billed separately."""
    cache_read = price.input if price.cache_read is None else price.cache_read
    cache_write = price.input if price.cache_write is None else price.cache_write
    return (
        input_tokens * price.input + output_tokens * price.output
        + cache_read_tokens * cache_read + cache_write_tokens * cache_write
    ) / 1_000_000


def vendor_estimates(tier: str, input_tokens: int, output_tokens: int) -> dict[str, float]:
    """What these tokens would cost on each vendor's default model for the tier, without caching."""
    return {vendor: priced_cost(VENDOR_TIER_PRICES[vendor][tier], input_tokens, output_tokens) for vendor in VENDORS}


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields: Any) -> None:
        context = current_request.get()
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "request_id": context.request_id if context else None,
            "user": context.user if context else None,
            "source": context.source if context else None,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        logger.info(line)

    def summary(self) -> dict[str, Any]:
        """Aggregate the audit file: cost per request and per agent, feedback counts."""
        totals: dict[str, Any] = {
            "requests": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "estimated_azure_cost_usd": 0.0, "feedback_good": 0, "feedback_bad": 0,
            "cost_usd_by_profile": defaultdict(float),
            "estimated_cost_usd_by_vendor": {vendor: 0.0 for vendor in VENDORS},
        }
        agents: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_azure_cost_usd": 0.0})
        if not self.path.exists():
            return {**totals, "cost_usd_by_profile": {}, "agents": {}}

        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = record.get("kind")
                if kind == "request_end":
                    totals["requests"] += 1
                    totals["failed"] += 0 if record.get("ok") else 1
                elif kind == "llm":
                    totals["input_tokens"] += record.get("input_tokens", 0)
                    totals["output_tokens"] += record.get("output_tokens", 0)
                    totals["cost_usd"] += record.get("cost_usd", 0.0)
                    totals["estimated_azure_cost_usd"] += record.get("estimated_azure_cost_usd", 0.0)
                    if record.get("cost_usd"):
                        totals["cost_usd_by_profile"][record.get("profile", "?")] += record["cost_usd"]
                    for vendor, cost in (record.get("estimated_cost_usd_by_vendor") or {}).items():
                        totals["estimated_cost_usd_by_vendor"][vendor] = totals["estimated_cost_usd_by_vendor"].get(vendor, 0.0) + cost
                    agent = agents[record.get("role", "?")]
                    agent["calls"] += 1
                    agent["input_tokens"] += record.get("input_tokens", 0)
                    agent["output_tokens"] += record.get("output_tokens", 0)
                    agent["estimated_azure_cost_usd"] += record.get("estimated_azure_cost_usd", 0.0)
                elif kind == "feedback":
                    totals["feedback_good" if record.get("rating") == "good" else "feedback_bad"] += 1
        totals["cost_usd_by_profile"] = dict(totals["cost_usd_by_profile"])
        return {**totals, "agents": dict(agents)}


def configure_telemetry(connection_string: str) -> bool:
    """Send logs and traces to Application Insights when a connection string is set."""
    if not connection_string:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError as exc:
        raise RuntimeError("Application Insights needs: pip install -e '.[azure]'") from exc
    configure_azure_monitor(connection_string=connection_string)
    logger.setLevel(logging.INFO)
    return True
