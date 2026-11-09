"""Read-only access to application logs.

Three backends share one interface: a local JSON-lines file (free), Azure Log
Analytics and Elasticsearch. Every message is masked before anything else sees it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from devassist.config import ConfigError, Settings
from devassist.masking import mask

_SERVICE_NAME = re.compile(r"^[\w.-]{1,100}$")
ERROR_LEVELS = ("ERROR", "CRITICAL", "FATAL")


@dataclass(frozen=True)
class LogEntry:
    timestamp: datetime
    level: str
    service: str
    message: str
    trace_id: str = ""


@dataclass
class ErrorGroup:
    signature: str
    count: int
    sample: str
    first_seen: datetime
    last_seen: datetime


class LogStore(Protocol):
    async def query(self, start: datetime, end: datetime, service: str | None = None, limit: int = 2000) -> list[LogEntry]: ...

    async def services(self) -> list[str]: ...


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class JsonlLogStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> list[LogEntry]:
        if not self.path.exists():
            return []
        entries = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                entries.append(LogEntry(
                    timestamp=_parse_time(record["timestamp"]),
                    level=record.get("level", "INFO").upper(),
                    service=record.get("service", "unknown"),
                    message=mask(record.get("message", "")),
                    trace_id=record.get("trace_id", ""),
                ))
        return entries

    async def query(self, start: datetime, end: datetime, service: str | None = None, limit: int = 2000) -> list[LogEntry]:
        matching = [e for e in self._read() if start <= e.timestamp <= end and (service is None or e.service == service)]
        return matching[:limit]

    async def services(self) -> list[str]:
        return sorted({entry.service for entry in self._read()})


class AzureLogAnalyticsStore:
    """Reads Application Insights data with a query-only credential (Log Analytics Reader)."""

    def __init__(self, workspace_id: str, client: Any = None) -> None:
        self.workspace_id = workspace_id
        if client is None:
            try:
                from azure.identity.aio import DefaultAzureCredential
                from azure.monitor.query.aio import LogsQueryClient
            except ImportError as exc:
                raise ConfigError("The azure log store needs: pip install -e '.[azure]'") from exc
            client = LogsQueryClient(DefaultAzureCredential())
        self.client = client

    @staticmethod
    def build_query(service: str | None, limit: int) -> str:
        where = ""
        if service:
            if not _SERVICE_NAME.match(service):
                raise ValueError(f"Invalid service name: {service!r}")
            where = f'| where AppRoleName == "{service}"\n'
        return (
            "AppTraces\n"
            f"{where}"
            "| project TimeGenerated, SeverityLevel, AppRoleName, Message, OperationId\n"
            "| order by TimeGenerated asc\n"
            f"| take {int(limit)}"
        )

    async def _run(self, query: str, timespan: tuple[datetime, datetime] | timedelta) -> list[list[Any]]:
        response = await self.client.query_workspace(self.workspace_id, query, timespan=timespan)
        return [list(row) for table in response.tables for row in table.rows]

    async def query(self, start: datetime, end: datetime, service: str | None = None, limit: int = 2000) -> list[LogEntry]:
        rows = await self._run(self.build_query(service, limit), (start, end))
        levels = {0: "DEBUG", 1: "INFO", 2: "WARNING", 3: "ERROR", 4: "CRITICAL"}
        return [LogEntry(when, levels.get(level, "INFO"), role, mask(message or ""), operation or "")
                for when, level, role, message, operation in rows]

    async def services(self) -> list[str]:
        rows = await self._run("AppTraces | distinct AppRoleName", timedelta(days=7))
        return sorted(row[0] for row in rows if row[0])


class ElasticsearchStore:
    """Uses only the _search endpoint. Give its API key the 'read' privilege and nothing else."""

    def __init__(self, url: str, api_key: str, index: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.index = index
        self.http = httpx.AsyncClient(base_url=url, headers={"Authorization": f"ApiKey {api_key}"}, timeout=30.0, transport=transport)

    async def _search(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.post(f"/{self.index}/_search", json=body)
        response.raise_for_status()
        return response.json()

    async def query(self, start: datetime, end: datetime, service: str | None = None, limit: int = 2000) -> list[LogEntry]:
        filters: list[dict[str, Any]] = [{"range": {"@timestamp": {"gte": start.isoformat(), "lte": end.isoformat()}}}]
        if service:
            filters.append({"term": {"service.name": service}})
        body = {"size": limit, "sort": [{"@timestamp": "asc"}], "query": {"bool": {"filter": filters}}}
        result = await self._search(body)
        entries = []
        for hit in result["hits"]["hits"]:
            source = hit["_source"]
            entries.append(LogEntry(
                timestamp=_parse_time(source["@timestamp"]),
                level=str(source.get("log", {}).get("level", "INFO")).upper(),
                service=source.get("service", {}).get("name", "unknown"),
                message=mask(source.get("message", "")),
                trace_id=source.get("trace", {}).get("id", ""),
            ))
        return entries

    async def services(self) -> list[str]:
        result = await self._search({"size": 0, "aggs": {"services": {"terms": {"field": "service.name", "size": 200}}}})
        return sorted(bucket["key"] for bucket in result["aggregations"]["services"]["buckets"])


def make_log_store(settings: Settings) -> LogStore:
    if settings.log_store == "azure":
        if not settings.log_analytics_workspace_id:
            raise ConfigError("DEVASSIST_LOG_STORE=azure needs LOG_ANALYTICS_WORKSPACE_ID.")
        return AzureLogAnalyticsStore(settings.log_analytics_workspace_id)
    if settings.log_store == "elasticsearch":
        if not settings.elastic_url:
            raise ConfigError("DEVASSIST_LOG_STORE=elasticsearch needs ELASTIC_URL.")
        return ElasticsearchStore(settings.elastic_url, settings.elastic_api_key, settings.elastic_index)
    return JsonlLogStore(settings.log_jsonl_path)


def group_errors(entries: list[LogEntry]) -> list[ErrorGroup]:
    """Group error lines by shape. Counts are measured here, never produced by the model."""
    counts: Counter[str] = Counter()
    groups: dict[str, ErrorGroup] = {}
    for entry in entries:
        if entry.level not in ERROR_LEVELS:
            continue
        signature = re.sub(r"<\w+>|\b0x[0-9a-f]+\b|\b\d+(?:\.\d+)?\b", "#", entry.message)[:200]
        counts[signature] += 1
        if signature not in groups:
            groups[signature] = ErrorGroup(signature, 0, entry.message, entry.timestamp, entry.timestamp)
        groups[signature].last_seen = entry.timestamp
    for signature, count in counts.items():
        groups[signature].count = count
    return sorted(groups.values(), key=lambda group: group.count, reverse=True)


_CLOCK = r"(\d{1,2})(?::(\d{2}))?"
_BETWEEN = re.compile(rf"(?:between|from|entre(?: las)?|de(?: las)?)\s+{_CLOCK}\s*(?:and|to|-|y(?: las)?|a(?: las)?)\s+{_CLOCK}", re.I)
_LAST = re.compile(r"(?:last|past|[úu]ltim[oa]s?)\s+(\d+)\s*(min|minute|minuto|hour|hora|day|d[ií]a)", re.I)


def parse_time_range(text: str, now: datetime | None = None, tz_name: str = "UTC") -> tuple[datetime, datetime]:
    """Understand 'yesterday between 17:00 and 18:00', 'last 2 hours', 'ayer'. Defaults to the last 24 h."""
    tz = ZoneInfo(tz_name)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    lowered = text.lower()

    last = _LAST.search(lowered)
    if last:
        unit = last.group(2)
        amount = int(last.group(1))
        delta = timedelta(minutes=amount) if unit.startswith("min") else timedelta(days=amount) if unit.startswith(("day", "d")) else timedelta(hours=amount)
        return now - delta, now

    day = None
    if re.search(r"\b(yesterday|ayer)\b", lowered):
        day = (now - timedelta(days=1)).date()
    elif re.search(r"\b(today|hoy)\b", lowered):
        day = now.date()

    between = _BETWEEN.search(lowered)
    if between and (between.group(2) or day is not None or ":" in between.group(0)):
        base = day or now.date()
        start_h, start_m, end_h, end_m = between.groups()
        if int(start_h) < 24 and int(end_h) < 24:
            start = datetime(base.year, base.month, base.day, int(start_h), int(start_m or 0), tzinfo=tz)
            end = datetime(base.year, base.month, base.day, int(end_h), int(end_m or 0), tzinfo=tz)
            if end <= start:
                end += timedelta(days=1)
            return start, end

    if day is not None:
        start = datetime(day.year, day.month, day.day, tzinfo=tz)
        return start, start + timedelta(days=1)
    return now - timedelta(hours=24), now
