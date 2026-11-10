"""Apply (or just check) the GitLab settings that keep AI agents from merging.

The DevAssist guarantee "agents never merge" does not depend on prompts here. GitLab enforces it:

- ``main``, ``develop`` and ``release/*`` are protected: push and merge allowed to Maintainers only,
  no force push. Duo flow service accounts get the Developer role, so they cannot merge or push there.
- Every merge request needs one approval, authors and committers cannot approve their own change,
  and approvals reset on new pushes (approval rules need Premium or Ultimate).
- Merge only when the pipeline passed and all threads are resolved.
- No account matching the AI pattern may be Maintainer or Owner of the project.

Usage (a Maintainer's token, read from the environment, never from arguments):

    GITLAB_URL=https://gitlab.example.com GITLAB_TOKEN=... python gitlab-duo/scripts/apply_guardrails.py group/project
    ... --apply   to change settings (without it, nothing is written)

REST endpoints used (GitLab API v4): protected_branches, approval_rules, approvals, projects, members/all.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

MAINTAINER = 40
DEVELOPER = 30
DEFAULT_BRANCHES = ("main", "develop", "release/*")
DEFAULT_BOT_PATTERN = r"^(ai-.+|duo-.+|gitlabduo)$"
APPROVAL_RULE = "Human review"
APPROVAL_SETTINGS = {
    "merge_requests_author_approval": False,
    "merge_requests_disable_committers_approval": True,
    "reset_approvals_on_push": True,
}
PROJECT_SETTINGS = {
    "only_allow_merge_if_pipeline_succeeds": True,
    "only_allow_merge_if_all_discussions_are_resolved": True,
}


@dataclass
class Report:
    actions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def action(self, text: str) -> None:
        self.actions.append(text)

    def problem(self, text: str) -> None:
        self.problems.append(text)


def _levels(entries: list[dict]) -> set[int]:
    return {e.get("access_level") for e in entries if e.get("access_level") is not None}


def protect_branches(http: httpx.Client, project: str, branches: tuple[str, ...], apply: bool, report: Report) -> None:
    existing = {b["name"]: b for b in http.get(f"/projects/{project}/protected_branches", params={"per_page": 100}).json()}
    for name in branches:
        current = existing.get(name)
        if current is None:
            report.action(f"protect {name}: push and merge Maintainers only, no force push")
            if apply:
                http.post(f"/projects/{project}/protected_branches", json={
                    "name": name, "push_access_level": MAINTAINER, "merge_access_level": MAINTAINER, "allow_force_push": False,
                }).raise_for_status()
            continue
        for kind in ("push", "merge"):
            levels = _levels(current.get(f"{kind}_access_levels", []))
            if any(level < MAINTAINER for level in levels):
                report.problem(f"{name}: {kind} is allowed below Maintainer {sorted(levels)}; set it to Maintainers in Settings > Repository.")
        if current.get("allow_force_push"):
            report.action(f"{name}: turn off force push")
            if apply:
                http.patch(f"/projects/{project}/protected_branches/{quote(name, safe='')}", json={"allow_force_push": False}).raise_for_status()


def require_approval(http: httpx.Client, project: str, apply: bool, report: Report) -> None:
    rules = http.get(f"/projects/{project}/approval_rules").json()
    if not any(rule.get("approvals_required", 0) >= 1 for rule in rules):
        report.action(f"add approval rule '{APPROVAL_RULE}': 1 approval required")
        if apply:
            http.post(f"/projects/{project}/approval_rules", json={"name": APPROVAL_RULE, "approvals_required": 1, "rule_type": "any_approver"}).raise_for_status()
    settings = http.get(f"/projects/{project}/approvals").json()
    wrong = {key: value for key, value in APPROVAL_SETTINGS.items() if settings.get(key) != value}
    if wrong:
        report.action("set approval settings " + ", ".join(f"{k}={v}" for k, v in wrong.items()))
        if apply:
            http.post(f"/projects/{project}/approvals", json=wrong).raise_for_status()


def merge_checks(http: httpx.Client, project: str, apply: bool, report: Report) -> None:
    current = http.get(f"/projects/{project}").json()
    wrong = {key: value for key, value in PROJECT_SETTINGS.items() if current.get(key) != value}
    if wrong:
        report.action("set merge checks " + ", ".join(f"{k}={v}" for k, v in wrong.items()))
        if apply:
            http.put(f"/projects/{project}", json=wrong).raise_for_status()


def check_ai_members(http: httpx.Client, project: str, bot_pattern: str, report: Report) -> None:
    members = http.get(f"/projects/{project}/members/all", params={"per_page": 100}).json()
    for member in members:
        username = member.get("username", "")
        if re.match(bot_pattern, username, re.IGNORECASE) and member.get("access_level", 0) > DEVELOPER:
            report.problem(f"AI account {username} has access level {member['access_level']}; it must be Developer (30) or lower.")


def run(http: httpx.Client, project_path: str, apply: bool, branches: tuple[str, ...] = DEFAULT_BRANCHES, bot_pattern: str = DEFAULT_BOT_PATTERN) -> Report:
    project = quote(project_path, safe="")
    report = Report()
    protect_branches(http, project, branches, apply, report)
    require_approval(http, project, apply, report)
    merge_checks(http, project, apply, report)
    check_ai_members(http, project, bot_pattern, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("project", help="group/project path")
    parser.add_argument("--apply", action="store_true", help="change settings; without it this is a dry run")
    parser.add_argument("--branches", default=",".join(DEFAULT_BRANCHES))
    args = parser.parse_args(argv)
    url = os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")
    token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        print("Set GITLAB_TOKEN (a Maintainer's token with the api scope).", file=sys.stderr)
        return 2
    with httpx.Client(base_url=f"{url}/api/v4", headers={"PRIVATE-TOKEN": token}, timeout=20) as http:
        report = run(http, args.project, args.apply, tuple(b for b in args.branches.split(",") if b), os.environ.get("AI_BOT_PATTERN") or DEFAULT_BOT_PATTERN)
    for action in report.actions:
        print(("applied: " if args.apply else "would apply: ") + action)
    for problem in report.problems:
        print("PROBLEM: " + problem)
    if not report.actions and not report.problems:
        print("All guardrails already in place.")
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
