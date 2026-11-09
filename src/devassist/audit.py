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
        }
        agents: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_azure_cost_usd": 0.0})
        if not self.path.exists():
            return {**totals, "agents": {}}

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
                    agent = agents[record.get("role", "?")]
                    agent["calls"] += 1
                    agent["input_tokens"] += record.get("input_tokens", 0)
                    agent["output_tokens"] += record.get("output_tokens", 0)
                    agent["estimated_azure_cost_usd"] += record.get("estimated_azure_cost_usd", 0.0)
                elif kind == "feedback":
                    totals["feedback_good" if record.get("rating") == "good" else "feedback_bad"] += 1
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
