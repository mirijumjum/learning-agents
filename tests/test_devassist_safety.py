"""The guarantees the design promises: no merges, masked data, bounded cost, safe paths."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from devassist.audit import AuditLog
from devassist.config import ConfigError, Settings
from devassist.gitlab import GitLabClient, GitLabError
from devassist.logstore import AzureLogAnalyticsStore, ElasticsearchStore, LogEntry, group_errors, parse_time_range
from devassist.masking import mask
from devassist.specialists import _code_without_docs, drop_unverified_urls, is_safe_test_path


def _client(tmp_path, handler=None, **overrides):
    def default(_request):
        return httpx.Response(200, json={})

    settings = Settings(audit_path=tmp_path / "audit.jsonl", **overrides)
    return GitLabClient(settings, AuditLog(settings.audit_path), transport=httpx.MockTransport(handler or default))


@pytest.mark.parametrize("method, path", [
    ("PUT", "/projects/a%2Fb/merge_requests/1/merge"),
    ("POST", "/projects/a%2Fb/merge_requests/1/merge"),
    ("DELETE", "/projects/a%2Fb/repository/branches/main"),
    ("DELETE", "/projects/a%2Fb"),
    ("POST", "/projects/a%2Fb/deployments"),
    ("POST", "/projects/a%2Fb/repository/tags"),
    ("POST", "/projects/a%2Fb/jobs/1/play"),
])
def test_merge_deploy_and_delete_never_leave_the_process(tmp_path, method, path):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(GitLabError, match="Blocked"):
        asyncio.run(_client(tmp_path, handler)._request(method, path))
    assert sent == []


def test_client_has_no_merge_or_delete_methods():
    names = [n for n in dir(GitLabClient) if not n.startswith("_")]
    assert not [n for n in names if "merge" in n and "merge_request" not in n]
    assert not [n for n in names if any(word in n for word in ("delete", "remove", "deploy_", "approve"))]


@pytest.mark.parametrize("branch", ["main", "develop", "feature/x", "AI/tests"])
def test_commits_only_reach_ai_branches(tmp_path, branch):
    with pytest.raises(GitLabError, match="Blocked"):
        asyncio.run(_client(tmp_path).commit_files("a/b", branch, "msg", {"f": "x"}, {"f": "create"}))


def test_project_allow_list_is_enforced(tmp_path):
    client = _client(tmp_path, gitlab_projects=("acme/payments-api",))
    with pytest.raises(GitLabError, match="not enabled"):
        asyncio.run(client.get_project("acme/secret-project"))
    assert asyncio.run(client.resolve_project("payments-api")) == "acme/payments-api"


def test_token_never_reaches_the_audit_trail(tmp_path):
    client = _client(tmp_path, gitlab_token="glpat-super-secret-value")
    asyncio.run(client.get_project("a/b"))
    assert "super-secret" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize("raw, leaked, kept", [
    ("failed for jane.doe@example.com at checkout", "jane.doe", "checkout"),
    ("client 10.0.4.17 disconnected", "10.0.4.17", "disconnected"),
    ("from 2001:db8::8a2e:370:7334 refused", "2001:db8", "refused"),
    ("call +34 612 345 678 back", "612 345", "back"),
    ("customer_id=C9001 not found", "C9001", "not found"),
    ("dni: 12345678Z rejected", "12345678Z", "rejected"),
    ("session 550e8400-e29b-41d4-a716-446655440000 expired", "550e8400", "expired"),
    ("card 4111 1111 1111 1111 declined", "4111", "declined"),
    ("Authorization: Bearer abc123def456ghi789", "abc123def", "Authorization"),
    ("password=hunter2 in config", "hunter2", "config"),
    ("token glpat-abcdefghij1234567890 used", "glpat-abc", "used"),
])
def test_personal_data_and_secrets_are_masked(raw, leaked, kept):
    masked = mask(raw)
    assert leaked not in masked and kept in masked


@pytest.mark.parametrize("line", [
    "2026-11-08 17:05:12 pipeline step finished",
    "std::vector add::foo failed",
    "tests/test_x.py:42: AssertionError",
    "timeout 3000 ms after 5 retries, build 20261108",
])
def test_ordinary_log_text_survives_masking(line):
    assert mask(line) == line


def test_fabricated_links_are_removed_and_real_ones_kept():
    real = "http://gitlab.local/a/-/merge_requests/1"
    text = f"See [the MR]({real}) and [this](https://gitlab.example.com/fake) and https://made.up/x done"
    cleaned = drop_unverified_urls(text, {real})
    assert real in cleaned and "gitlab.example.com" not in cleaned and "made.up" not in cleaned
    assert "this" in cleaned


@pytest.mark.parametrize("path, safe", [
    ("tests/test_order.py", True), ("src/test/java/OrderTest.java", True), ("test_order.py", True),
    ("app/order_service.py", False), (".gitlab-ci.yml", False), ("../tests/test_x.py", False),
    ("/etc/tests/test_x.py", False), ("tests/.gitlab-ci.yml", False),
])
def test_test_agent_only_writes_test_files(path, safe):
    assert is_safe_test_path(path) is safe


def test_comment_guard_accepts_docstrings_and_rejects_code_changes():
    original = "def add(a, b):\n    return a + b\n"
    assert _code_without_docs(original) == _code_without_docs('"""Maths."""\ndef add(a, b):\n    """Add."""\n    return a + b\n')
    assert _code_without_docs(original) != _code_without_docs('def add(a, b):\n    """Add."""\n    return a - b\n')


def _at(minute, message, level="ERROR"):
    return LogEntry(datetime(2026, 1, 1, 12, minute, tzinfo=timezone.utc), level, "svc", message)


def test_errors_are_counted_by_shape():
    groups = group_errors([_at(1, "timeout after 3000 ms"), _at(2, "timeout after 5000 ms"), _at(3, "disk full"), _at(4, "all fine", "INFO")])
    assert [(g.count, g.sample) for g in groups] == [(2, "timeout after 3000 ms"), (1, "disk full")]


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("text, start, end", [
    ("why did it fail yesterday between 17:00 and 18:00", (8, 17, 0), (8, 18, 0)),
    ("errores de ayer entre las 17:30 y las 18:15", (8, 17, 30), (8, 18, 15)),
    ("what failed today from 09:00 to 10:00", (9, 9, 0), (9, 10, 0)),
])
def test_time_windows(text, start, end):
    got_start, got_end = parse_time_range(text, now=NOW)
    assert (got_start.day, got_start.hour, got_start.minute) == start
    assert (got_end.day, got_end.hour, got_end.minute) == end


def test_relative_and_default_windows():
    assert parse_time_range("errors in the last 2 hours", now=NOW) == (NOW - timedelta(hours=2), NOW)
    assert parse_time_range("did anything break?", now=NOW) == (NOW - timedelta(hours=24), NOW)


def test_azure_query_rejects_injection():
    with pytest.raises(ValueError):
        AzureLogAnalyticsStore.build_query('x" | union SecretTable', 10)
    assert 'AppRoleName == "checkout"' in AzureLogAnalyticsStore.build_query("checkout", 10)


def test_elasticsearch_store_masks_results():
    def handler(request):
        assert request.url.path.endswith("/_search") and request.method == "POST"
        return httpx.Response(200, json={"hits": {"hits": [{"_source": {
            "@timestamp": "2026-01-01T12:00:00Z", "log": {"level": "error"}, "service": {"name": "svc"}, "message": "bad user a@b.com",
        }}]}})

    store = ElasticsearchStore("http://es", "key", "logs-*", transport=httpx.MockTransport(handler))
    entries = asyncio.run(store.query(NOW - timedelta(hours=1), NOW))
    assert entries[0].level == "ERROR" and "a@b.com" not in entries[0].message


def test_azure_profile_requires_endpoint():
    with pytest.raises(ConfigError, match="AZURE_OPENAI_ENDPOINT"):
        Settings.from_env({"DEVASSIST_PROFILE": "azure"})


def test_unknown_profile_is_rejected():
    with pytest.raises(ConfigError):
        Settings.from_env({"DEVASSIST_PROFILE": "gemini"})
