"""Release gate: sample requests that must keep working before instructions or models change.

Runs every case against the local GitLab stand-in and checks what really
happened (route taken, branch created, MR opened), not just the reply text.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from aiohttp import web

from devassist.audit import RequestContext
from devassist.config import Settings
from devassist.dev_gitlab import FakeGitLab, seed_sample_logs
from devassist.runtime import build_devassist


@dataclass
class CaseResult:
    id: str
    passed: bool
    detail: str


def load_cases() -> list[dict[str, Any]]:
    return yaml.safe_load(resources.files("devassist").joinpath("eval_cases.yaml").read_text(encoding="utf-8"))


def _check(case: dict[str, Any], route: str, text: str, fake: FakeGitLab) -> str:
    """Return the reason a case failed, or an empty string."""
    if route != case["route"]:
        return f"routed to {route!r}, expected {case['route']!r}"
    lowered = text.lower()
    for needle in case.get("contains", []):
        if needle.lower() not in lowered:
            return f"reply lacks {needle!r}: {text[:300]}"
    for needle in case.get("not_contains", []):
        if needle.lower() in lowered:
            return f"reply must not contain {needle!r}: {text[:300]}"
    expect = case.get("expect", {})
    project = fake.projects.get(expect.get("project", ""))
    if "branch" in expect and expect["branch"] not in project["branches"]:  # type: ignore[index]
        return f"branch {expect['branch']} was not created"
    if expect.get("ai_mr") and not any(mr["source_branch"].startswith("ai/") for mr in project["mrs"].values()):  # type: ignore[union-attr]
        return "no merge request was opened from an ai/* branch"
    return ""


async def run_evals(settings: Settings, case_filter: str = "", progress: Any = None) -> list[CaseResult]:
    results: list[CaseResult] = []
    cases = [c for c in load_cases() if case_filter in c["id"]]
    with tempfile.TemporaryDirectory(prefix="devassist-eval-") as tmp:
        root = Path(tmp)
        seed_sample_logs(root / "logs.jsonl")
        fake = FakeGitLab()
        runner = web.AppRunner(fake.app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        fake.base_url = f"http://127.0.0.1:{port}"
        run_settings = replace(
            settings, gitlab_url=fake.base_url, gitlab_token="eval", gitlab_projects=("acme/payments-api", "acme/customer-portal"),
            log_store="jsonl", log_jsonl_path=root / "logs.jsonl", audit_path=root / "audit.jsonl", pipeline_poll_seconds=0.2, timezone="UTC",
        )
        assistant = build_devassist(run_settings)
        original_route = assistant.route
        taken: dict[str, str] = {}

        async def spy(text: str) -> str:
            taken["route"] = await original_route(text)
            return taken["route"]

        assistant.route = spy  # type: ignore[method-assign]
        try:
            for case in cases:
                taken.clear()
                context = RequestContext(user="dev", email="dev@acme.test", conversation_id=case["id"], source="eval")
                try:
                    reply = await assistant.handle(case["request"], context)
                    reason = _check(case, taken.get("route", ""), reply.render(), fake)
                except Exception as exc:  # a crash fails the case, and the rest still run
                    reason = f"{type(exc).__name__}: {exc}"
                result = CaseResult(case["id"], not reason, reason)
                results.append(result)
                if progress:
                    progress(result)
        finally:
            await assistant.gitlab.aclose()
            await asyncio.sleep(0)
            await runner.cleanup()
    return results
