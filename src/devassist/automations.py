"""Runs the agents from GitLab webhooks, without anyone asking."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from devassist.audit import RequestContext, current_request
from devassist.gitlab import AI_BRANCH_PREFIX
from devassist.orchestrator import DevAssist
from devassist.specialists import AgentRefusal, Reply
from devassist.webhooks import RecentDeliveries

logger = logging.getLogger("devassist.automations")


class Automations:
    def __init__(self, devassist: DevAssist) -> None:
        self.devassist = devassist
        self.settings = devassist.settings
        self._deliveries = RecentDeliveries()
        self._tasks: set[asyncio.Task] = set()

    def is_duplicate(self, delivery_id: str) -> bool:
        """GitLab retries deliveries, and a retry must not repeat the work."""
        return self._deliveries.is_duplicate(delivery_id)

    def plan(self, event: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """Decide what to do. Events from ai/* branches are ignored, or agents would trigger themselves forever."""
        project = payload.get("project", {}).get("path_with_namespace")
        attributes = payload.get("object_attributes", {})
        if not project:
            return None
        if event == "Merge Request Hook":
            if attributes.get("source_branch", "").startswith(AI_BRANCH_PREFIX):
                return None
            if attributes.get("action") == "open" and self.settings.auto_tests_on_mr:
                return "tests", {"project": project, "iid": attributes["iid"]}
            if attributes.get("action") == "merge" and self.settings.auto_changelog_on_merge:
                return "changelog", {"project": project, "iid": attributes["iid"], "target": attributes.get("target_branch")}
        if event == "Pipeline Hook" and self.settings.auto_trace_on_pipeline_failure:
            if attributes.get("status") == "failed" and not attributes.get("ref", "").startswith(AI_BRANCH_PREFIX):
                merge_request = payload.get("merge_request") or {}
                return "trace", {"project": project, "pipeline_id": attributes["id"], "iid": merge_request.get("iid")}
        return None

    async def _notify_teams(self, text: str) -> None:
        if not self.settings.teams_notify_url:
            return
        async with httpx.AsyncClient(timeout=20) as http:
            await http.post(self.settings.teams_notify_url, json={"text": text})

    async def execute(self, kind: str, args: dict[str, Any]) -> Reply:
        context = RequestContext(user="gitlab-webhook", source="webhook", conversation_id=f"webhook:{kind}")
        token = current_request.set(context)
        audit = self.devassist.audit
        audit.event("request_start", text=f"automation:{kind} {args}")
        ok = True
        try:
            project = args["project"]
            if kind == "tests":
                reply = (await self.devassist.test.run(project, mr_iid=args["iid"])).reply
            elif kind == "changelog":
                reply = await self.devassist.docs.changelog(project, args.get("target"))
            else:
                reply = (await self.devassist.trace.pipeline(project, args["pipeline_id"])).reply
                reply.text = reply.text.replace("\n\nSay **open the issue** and I'll file this in GitLab.", "")
            if args.get("iid"):
                await self.devassist.gitlab.comment_on_merge_request(project, args["iid"], f"**DevAssist**\n\n{reply.render()}")
            await self._notify_teams(f"DevAssist ({project}): {reply.render()}")
            return reply
        except AgentRefusal as exc:
            reply = Reply(str(exc))
            if args.get("iid"):
                await self.devassist.gitlab.comment_on_merge_request(args["project"], args["iid"], f"**DevAssist**\n\n{exc}")
            return reply
        except Exception as exc:
            ok = False
            audit.event("error", error=f"{type(exc).__name__}: {exc}"[:500])
            logger.exception("Automation %s failed", kind)
            raise
        finally:
            audit.event("request_end", route=f"automation:{kind}", ok=ok)
            current_request.reset(token)

    def start(self, kind: str, args: dict[str, Any]) -> asyncio.Task:
        async def guarded() -> None:
            try:
                await self.execute(kind, args)
            except Exception:  # already audited and logged; the webhook was acknowledged long ago
                pass

        task = asyncio.create_task(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
