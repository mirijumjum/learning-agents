"""GitLab Duo → Microsoft Teams bridge.

GitLab Duo flows and agents publish their results inside GitLab: comments on
merge requests and issues, merge requests from their own branches, pipelines.
GitLab's built-in Microsoft Teams integration can forward *all* comments, merge
requests and pipelines of a project, which is too noisy for a team channel.

This small service receives GitLab webhooks and forwards only what the AI did:

- comments written by the Duo service accounts (``ai-<flow>-<group>``) or by ``@GitLabDuo``;
- merge requests opened, merged or closed from ``ai/*`` branches;
- finished pipelines (passed or failed) of ``ai/*`` branches.

It posts an Adaptive Card to a Teams *Workflows* webhook ("Post to a channel when
a webhook request is received"). It never writes to GitLab and needs no GitLab
token: the only secret it holds is the webhook secret. Text is masked with
``devassist.masking`` before it leaves for Teams.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from aiohttp import web
from aiohttp.web import Application, Request, Response, json_response

from devassist.masking import mask
from devassist.webhooks import RecentDeliveries, check_gitlab_secret, delivery_id

logger = logging.getLogger("devassist.duo_bridge")

DEFAULT_AUTHORS = r"^(ai-.+|duo-.+|gitlabduo)$"
MAX_TEXT = 1500


@dataclass(frozen=True)
class BridgeSettings:
    webhook_secret: str = ""
    teams_url: str = ""
    author_pattern: str = DEFAULT_AUTHORS
    branch_prefix: str = "ai/"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BridgeSettings":
        variables = env if env is not None else os.environ
        return cls(
            webhook_secret=variables.get("GITLAB_WEBHOOK_SECRET", ""),
            teams_url=variables.get("TEAMS_NOTIFY_WEBHOOK_URL", ""),
            author_pattern=variables.get("DUO_BRIDGE_AUTHORS", DEFAULT_AUTHORS) or DEFAULT_AUTHORS,
            branch_prefix=variables.get("DUO_BRIDGE_BRANCH_PREFIX", "ai/") or "ai/",
        )

    def is_ai_author(self, username: str) -> bool:
        return bool(username) and re.match(self.author_pattern, username, re.IGNORECASE) is not None


@dataclass(frozen=True)
class Notification:
    title: str
    text: str
    url: str = ""


def _shorten(text: str) -> str:
    text = mask(text.strip())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1].rstrip() + "…"


def _note(payload: dict[str, Any], settings: BridgeSettings) -> Notification | None:
    author = (payload.get("user") or {}).get("username", "")
    if not settings.is_ai_author(author):
        return None
    note = payload.get("object_attributes") or {}
    project = (payload.get("project") or {}).get("path_with_namespace", "?")
    if payload.get("merge_request"):
        target = f"!{payload['merge_request'].get('iid')} {payload['merge_request'].get('title', '')}".strip()
    elif payload.get("issue"):
        target = f"#{payload['issue'].get('iid')} {payload['issue'].get('title', '')}".strip()
    else:
        target = note.get("noteable_type", "item")
    return Notification(f"{author} on {project} {target}", _shorten(note.get("note", "")), note.get("url", ""))


def _merge_request(payload: dict[str, Any], settings: BridgeSettings) -> Notification | None:
    attributes = payload.get("object_attributes") or {}
    if not attributes.get("source_branch", "").startswith(settings.branch_prefix):
        return None
    verbs = {"open": "opened", "merge": "merged", "close": "closed", "reopen": "reopened"}
    action = attributes.get("action", "")
    if action not in verbs:
        return None
    project = (payload.get("project") or {}).get("path_with_namespace", "?")
    who = (payload.get("user") or {}).get("username", "?")
    text = f"{attributes.get('source_branch')} → {attributes.get('target_branch')}, {verbs[action]} by {who}."
    if action == "open":
        text += " A person must review and merge it."
    return Notification(f"AI merge request {verbs[action]}: {project} !{attributes.get('iid')} {attributes.get('title', '')}".strip(), mask(text), attributes.get("url", ""))


def _pipeline(payload: dict[str, Any], settings: BridgeSettings) -> Notification | None:
    attributes = payload.get("object_attributes") or {}
    status = attributes.get("status", "")
    if not attributes.get("ref", "").startswith(settings.branch_prefix) or status not in ("success", "failed"):
        return None
    project = payload.get("project") or {}
    url = attributes.get("url") or (f"{project['web_url']}/-/pipelines/{attributes.get('id')}" if project.get("web_url") else "")
    failed = [b.get("name", "?") for b in payload.get("builds") or [] if b.get("status") == "failed"]
    parts = [f"Pipeline #{attributes.get('id')} on {attributes.get('ref')}: {'passed' if status == 'success' else 'failed'}."]
    if attributes.get("coverage") not in (None, ""):
        parts.append(f"Coverage {attributes['coverage']}%.")
    if failed:
        parts.append("Failed jobs: " + ", ".join(sorted(failed)) + ".")
    return Notification(f"{project.get('path_with_namespace', '?')}: AI branch pipeline {'passed' if status == 'success' else 'failed'}", " ".join(parts), url)


_HANDLERS: dict[str, Callable[[dict[str, Any], BridgeSettings], Notification | None]] = {
    "Note Hook": _note,
    "Merge Request Hook": _merge_request,
    "Pipeline Hook": _pipeline,
}


def plan_notification(event: str, payload: dict[str, Any], settings: BridgeSettings) -> Notification | None:
    """What to post in Teams for this GitLab event, or None to stay quiet."""
    handler = _HANDLERS.get(event)
    return handler(payload, settings) if handler else None


def teams_payload(notification: Notification) -> dict[str, Any]:
    """The envelope the Teams Workflows webhook template expects: one Adaptive Card attachment."""
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": notification.title, "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": notification.text or "(no text)", "wrap": True},
        ],
    }
    if notification.url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in GitLab", "url": notification.url}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


Poster = Callable[[str, dict[str, Any]], Awaitable[None]]


async def post_to_teams(url: str, body: dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=8) as http:
        response = await http.post(url, json=body)
        response.raise_for_status()


def create_bridge_app(settings: BridgeSettings, post: Poster = post_to_teams) -> Application:
    deliveries = RecentDeliveries()

    async def gitlab_webhook(request: Request) -> Response:
        rejected = check_gitlab_secret(request.headers, settings.webhook_secret)
        if rejected:
            return json_response({"error": rejected[1]}, status=rejected[0])
        delivery = delivery_id(request.headers)
        if deliveries.is_duplicate(delivery):
            return json_response({"status": "duplicate"})
        try:
            payload: dict[str, Any] = await request.json()
        except ValueError:
            return json_response({"error": "invalid JSON"}, status=400)
        notification = plan_notification(request.headers.get("X-Gitlab-Event", ""), payload, settings)
        if notification is None:
            return json_response({"status": "ignored"})
        if not settings.teams_url:
            return json_response({"status": "ignored", "reason": "TEAMS_NOTIFY_WEBHOOK_URL is not set"})
        try:
            await post(settings.teams_url, teams_payload(notification))
        except Exception as exc:  # let GitLab retry: forget this delivery
            deliveries.forget(delivery)
            logger.warning("Teams post failed: %s", exc)
            return json_response({"error": "could not post to Teams"}, status=502)
        return json_response({"status": "posted", "title": notification.title}, status=202)

    async def health(_: Request) -> Response:
        return json_response({"status": "ok", "teams": bool(settings.teams_url), "webhooks": bool(settings.webhook_secret)})

    app = Application()
    app.add_routes([web.post("/webhooks/gitlab", gitlab_webhook), web.get("/healthz", health)])
    return app
