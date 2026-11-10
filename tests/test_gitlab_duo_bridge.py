"""The GitLab Duo → Teams bridge and the guardrails script."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
from aiohttp import ClientSession, web

from devassist.duo_bridge import BridgeSettings, Notification, create_bridge_app, plan_notification, teams_payload

SETTINGS = BridgeSettings(webhook_secret="s3cret", teams_url="https://teams.example/workflow")
PROJECT = {"path_with_namespace": "acme/payments-api", "web_url": "https://gitlab.example/acme/payments-api"}


def _note(username, text="4 tests added. Pipeline passed.", target="merge_request"):
    payload = {
        "user": {"username": username},
        "project": PROJECT,
        "object_attributes": {"note": text, "noteable_type": "MergeRequest", "url": "https://gitlab.example/acme/payments-api/-/merge_requests/231#note_1"},
    }
    payload[target] = {"iid": 231, "title": "Order discount"}
    return payload


def test_only_ai_comments_are_forwarded():
    assert plan_notification("Note Hook", _note("alice"), SETTINGS) is None
    for author in ("ai-devassist-tests-acme", "GitLabDuo", "duo-review"):
        notification = plan_notification("Note Hook", _note(author), SETTINGS)
        assert notification and notification.title == f"{author} on acme/payments-api !231 Order discount"
    issue = plan_notification("Note Hook", _note("ai-devassist-docs-acme", target="issue"), SETTINGS)
    assert issue.title.endswith("#231 Order discount")


def test_personal_data_is_masked_before_it_reaches_teams():
    notification = plan_notification("Note Hook", _note("ai-x", "Failed for jane.doe@example.com from 10.1.2.3"), SETTINGS)
    assert "jane.doe@example.com" not in notification.text and "10.1.2.3" not in notification.text


def test_ai_merge_requests_and_pipelines():
    mr = {"user": {"username": "ai-devassist-tests-acme"}, "project": PROJECT,
          "object_attributes": {"action": "open", "iid": 240, "title": "Draft: AI tests for !231", "source_branch": "ai/tests-231", "target_branch": "feature/order-discount", "url": "u"}}
    notification = plan_notification("Merge Request Hook", mr, SETTINGS)
    assert notification.title == "AI merge request opened: acme/payments-api !240 Draft: AI tests for !231"
    assert "A person must review and merge it." in notification.text
    human = {**mr, "object_attributes": {**mr["object_attributes"], "source_branch": "feature/x"}}
    assert plan_notification("Merge Request Hook", human, SETTINGS) is None
    assert plan_notification("Merge Request Hook", {**mr, "object_attributes": {**mr["object_attributes"], "action": "update"}}, SETTINGS) is None

    pipeline = {"project": PROJECT, "object_attributes": {"id": 5600, "ref": "ai/tests-231", "status": "failed", "coverage": "81.5"},
                "builds": [{"name": "unit-tests", "status": "failed"}, {"name": "ai-branch-guard", "status": "success"}]}
    notification = plan_notification("Pipeline Hook", pipeline, SETTINGS)
    assert notification.text == "Pipeline #5600 on ai/tests-231: failed. Coverage 81.5%. Failed jobs: unit-tests."
    assert notification.url == "https://gitlab.example/acme/payments-api/-/pipelines/5600"
    running = {**pipeline, "object_attributes": {**pipeline["object_attributes"], "status": "running"}}
    assert plan_notification("Pipeline Hook", running, SETTINGS) is None
    assert plan_notification("Push Hook", pipeline, SETTINGS) is None


def test_teams_payload_is_an_adaptive_card_envelope():
    body = teams_payload(Notification("Title", "Text", "https://gitlab.example/x"))
    assert body["type"] == "message"
    attachment = body["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["body"][0]["text"] == "Title"
    assert card["actions"] == [{"type": "Action.OpenUrl", "title": "Open in GitLab", "url": "https://gitlab.example/x"}]
    assert "actions" not in teams_payload(Notification("T", "x"))["attachments"][0]["content"]


def test_long_comments_are_shortened():
    notification = plan_notification("Note Hook", _note("ai-x", "a" * 5000), SETTINGS)
    assert len(notification.text) == 1500 and notification.text.endswith("…")


async def _serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _headers(secret="s3cret", event="Note Hook", delivery="d1"):
    return {"X-Gitlab-Token": secret, "X-Gitlab-Event": event, "Idempotency-Key": delivery}


def test_bridge_over_http():
    posted = []
    failures = [RuntimeError("Teams is down")]

    async def fake_post(url, body):
        if failures:
            raise failures.pop()
        posted.append((url, body))

    async def run():
        runner, url = await _serve(create_bridge_app(SETTINGS, post=fake_post))
        async with ClientSession() as http:
            assert (await http.post(f"{url}/webhooks/gitlab", json=_note("ai-x"), headers=_headers(secret="nope"))).status == 401
            assert (await http.post(f"{url}/webhooks/gitlab", json=_note("alice"), headers=_headers(delivery="human"))).status == 200
            first = await http.post(f"{url}/webhooks/gitlab", json=_note("ai-x"), headers=_headers(delivery="d1"))
            assert first.status == 502  # Teams failed: GitLab will retry
            retry = await http.post(f"{url}/webhooks/gitlab", json=_note("ai-x"), headers=_headers(delivery="d1"))
            assert retry.status == 202
            duplicate = await http.post(f"{url}/webhooks/gitlab", json=_note("ai-x"), headers=_headers(delivery="d1"))
            assert (await duplicate.json())["status"] == "duplicate"
            assert (await (await http.get(f"{url}/healthz")).json()) == {"status": "ok", "teams": True, "webhooks": True}
        await runner.cleanup()

        closed = BridgeSettings(teams_url="https://teams.example/workflow")
        runner, url = await _serve(create_bridge_app(closed, post=fake_post))
        async with ClientSession() as http:
            assert (await http.post(f"{url}/webhooks/gitlab", json=_note("ai-x"), headers=_headers())).status == 503
        await runner.cleanup()

    asyncio.run(run())
    assert len(posted) == 1 and posted[0][0] == SETTINGS.teams_url


def test_settings_from_env():
    settings = BridgeSettings.from_env({"GITLAB_WEBHOOK_SECRET": "x", "TEAMS_NOTIFY_WEBHOOK_URL": "https://t", "DUO_BRIDGE_AUTHORS": "^bot$"})
    assert settings.is_ai_author("BOT") and not settings.is_ai_author("ai-flow")


# ---- apply_guardrails.py --------------------------------------------------

_SPEC = importlib.util.spec_from_file_location("apply_guardrails", Path(__file__).resolve().parents[1] / "gitlab-duo" / "scripts" / "apply_guardrails.py")
guardrails = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = guardrails
_SPEC.loader.exec_module(guardrails)


class FakeGitLab:
    """Just enough of the GitLab REST API for the guardrails script."""

    def __init__(self):
        self.protected = [{"name": "main", "push_access_levels": [{"access_level": 30}], "merge_access_levels": [{"access_level": 40}], "allow_force_push": True}]
        self.rules = []
        self.approvals = {"merge_requests_author_approval": True, "merge_requests_disable_committers_approval": False, "reset_approvals_on_push": True}
        self.project = {"only_allow_merge_if_pipeline_succeeds": False, "only_allow_merge_if_all_discussions_are_resolved": True}
        self.members = [{"username": "alice", "access_level": 40}, {"username": "ai-devassist-tests-acme", "access_level": 30}]
        self.writes = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0].removeprefix("/api/v4/projects/acme%2Fpayments-api")
        if request.method != "GET":
            self.writes.append((request.method, path, request.content and json.loads(request.content)))
        if request.method == "GET":
            data = {"/protected_branches": self.protected, "/approval_rules": self.rules, "/approvals": self.approvals,
                    "": self.project, "/members/all": self.members}[path]
            return httpx.Response(200, json=data)
        return httpx.Response(201, json={})

    def client(self):
        return httpx.Client(base_url="https://gitlab.example/api/v4", transport=httpx.MockTransport(self.handler))


def test_guardrails_dry_run_writes_nothing_and_reports_problems():
    fake = FakeGitLab()
    with fake.client() as http:
        report = guardrails.run(http, "acme/payments-api", apply=False)
    assert fake.writes == []
    assert report.problems == ["main: push is allowed below Maintainer [30]; set it to Maintainers in Settings > Repository."]
    assert "protect develop: push and merge Maintainers only, no force push" in report.actions
    assert "main: turn off force push" in report.actions
    assert any(a.startswith("add approval rule") for a in report.actions)


def test_guardrails_apply_sends_the_documented_calls():
    fake = FakeGitLab()
    fake.members.append({"username": "ai-devassist-docs-acme", "access_level": 40})
    with fake.client() as http:
        report = guardrails.run(http, "acme/payments-api", apply=True)
    writes = {(m, p): body for m, p, body in fake.writes}
    assert writes[("POST", "/protected_branches")]["merge_access_level"] == 40  # last one: release/*
    assert writes[("PATCH", "/protected_branches/main")] == {"allow_force_push": False}
    assert writes[("POST", "/approval_rules")] == {"name": "Human review", "approvals_required": 1, "rule_type": "any_approver"}
    assert writes[("POST", "/approvals")] == {"merge_requests_author_approval": False, "merge_requests_disable_committers_approval": True}
    assert writes[("PUT", "")] == {"only_allow_merge_if_pipeline_succeeds": True}
    assert any("ai-devassist-docs-acme" in p for p in report.problems)


def test_guardrails_cli_needs_a_token(monkeypatch, capsys):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    assert guardrails.main(["acme/payments-api"]) == 2
    assert "GITLAB_TOKEN" in capsys.readouterr().err
