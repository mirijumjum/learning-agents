"""The tools the Copilot Studio agent calls, defined once for MCP and for REST.

The rule of the Foundry design still holds: facts are measured by code and the
model only adds judgement. The model writes tests, docs and diagnoses; whether
tests pass, how many errors there were, and every link come from these tools.

Every write goes through ``devassist.gitlab.GitLabClient``, which has no merge,
deploy or delete method, allow-lists each GitLab endpoint, only commits to ai/*
branches and only touches allow-listed projects. Log lines and CI logs are
masked before they are returned, and so is any text the agent posts to GitLab.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from devassist.audit import AuditLog, RequestContext, current_request
from devassist.config import Settings
from devassist.gitlab import GitLabClient, GitLabError
from devassist.logstore import LogStore, group_errors, parse_time_range
from devassist.masking import mask
from devassist.specialists import (
    _API_FILE,
    _FENCE,
    _TEST_CASE,
    SOURCE_EXTENSIONS,
    AgentRefusal,
    DocsAgent,
    TestAgent,
    _code_without_docs,
    _tail,
    _unique_branch,
    is_safe_test_path,
)
from devassist_copilot.sessions import FINISHED, SessionStore, TestSession

MAX_WAIT_SECONDS = 25  # one tool call must stay well under the connector timeout
MAX_DOC_CHARS = 100_000
_BRANCH_NAME = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<![./])$")
_FILE_FORMAT = (
    "Pass the complete content of ONE test file in SubmitTestFile. Put it in the tests folder and follow the style "
    "example. Do not change production code. Import the real modules; do not mock the code under test."
)


class ToolError(RuntimeError):
    """A refusal or failure the agent should read and explain to the user."""


@dataclass
class Caller:
    """Who is asking. From Entra ID (Easy Auth) in production, or asserted by the agent with an API key."""

    email: str = ""


@dataclass(frozen=True)
class Tool:
    name: str  # MCP tool name and OpenAPI operationId
    title: str  # short summary shown to makers
    description: str  # what the model reads to decide when to call it
    params: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    output: dict[str, dict[str, Any]]
    read_only: bool
    handler: Callable[["Backend", dict[str, Any], Caller], Awaitable[dict[str, Any]]] = field(repr=False)

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.params, "required": list(self.required), "additionalProperties": False}


# ---------------------------------------------------------------- schema helpers
def _s(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _i(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **extra}


def _b(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _obj_list(description: str, item: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": "object", "properties": item}}


PROJECT = _s("GitLab project, e.g. payments-api or acme/payments-api. Only enabled projects work.")
LINK = _s("Link that came from GitLab")
SUMMARY = _s("Result written by the backend from measured facts. Show it to the user as is.")
FILES = {"path": _s("File path"), "content": _s("File content")}


# ---------------------------------------------------------------- backend
class Backend:
    def __init__(self, settings: Settings, audit: AuditLog, gitlab: GitLabClient, logs: LogStore, sessions: SessionStore) -> None:
        self.settings, self.audit, self.gitlab, self.logs, self.sessions = settings, audit, gitlab, logs, sessions
        # The specialists' deterministic helpers are reused as they are; their model is never called here.
        self._tests = TestAgent(None, gitlab, settings)  # type: ignore[arg-type]
        self._docs = DocsAgent(None, gitlab, settings)  # type: ignore[arg-type]

    async def call(self, name: str, arguments: dict[str, Any] | None, caller: Caller) -> dict[str, Any]:
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool {name!r}.")
        args = validate_arguments(tool, arguments or {})
        token = current_request.set(RequestContext(user=caller.email or "copilot-studio", email=caller.email, source="copilot-studio"))
        started = time.monotonic()
        try:
            result = await tool.handler(self, args, caller)
        except (GitLabError, AgentRefusal) as exc:
            self.audit.event("tool_call", tool=name, ok=False, error=str(exc)[:300], ms=int((time.monotonic() - started) * 1000))
            raise ToolError(str(exc)) from exc
        except ToolError as exc:
            self.audit.event("tool_call", tool=name, ok=False, error=str(exc)[:300], ms=int((time.monotonic() - started) * 1000))
            raise
        finally:
            current_request.reset(token)
        self.audit.event("tool_call", tool=name, ok=True, ms=int((time.monotonic() - started) * 1000), user=caller.email or None)
        return result

    # ---- shared helpers
    async def project(self, name: str) -> str:
        name = (name or self.settings.default_project).strip()
        if not name:
            raise ToolError("Say which project. Call ListProjects to see the enabled ones.")
        return await self.gitlab.resolve_project(name)

    def username(self, caller: Caller) -> str:
        if not caller.email:
            raise ToolError("I don't know who you are, so I can't filter by 'assigned to me'. Name the GitLab user instead.")
        return self.settings.user_map.get(caller.email) or caller.email.split("@")[0]

    def session(self, session_id: str, caller: Caller, *, owner_only: bool = False) -> TestSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise ToolError(f"No test session {session_id!r}. Start one with StartTestSession.")
        if owner_only and session.created_by and caller.email and caller.email != session.created_by:
            raise ToolError("This test session belongs to someone else.")
        return session

    def footer(self, caller: Caller) -> str:
        who = f" on behalf of {caller.email}" if caller.email else ""
        return f"\n\n_Posted by DevAssist{who}._"


def _coerce(value: Any, schema: dict[str, Any], name: str) -> Any:
    kind = schema.get("type")
    if kind == "integer":
        if isinstance(value, bool):
            raise ToolError(f"{name} must be a whole number.")
        if isinstance(value, str):
            value = value.strip().lstrip("!#")
            if not value.isdigit():
                raise ToolError(f"{name} must be a whole number, got {value!r}.")
            value = int(value)
        if not isinstance(value, int):
            raise ToolError(f"{name} must be a whole number.")
        low, high = schema.get("minimum"), schema.get("maximum")
        if low is not None and value < low or high is not None and value > high:
            raise ToolError(f"{name} must be between {low} and {high}.")
        return value
    if kind == "boolean":
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        if not isinstance(value, bool):
            raise ToolError(f"{name} must be true or false.")
        return value
    if kind == "string":
        if not isinstance(value, str):
            raise ToolError(f"{name} must be text.")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolError(f"{name} must be one of {', '.join(schema['enum'])}.")
        return value
    if kind == "array":
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ToolError(f"{name} must be a list of objects.")
        return value
    return value


def validate_arguments(tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
    """Check types and required fields. Unknown fields are ignored, since channels sometimes add their own."""
    if not isinstance(arguments, dict):
        raise ToolError("Arguments must be an object.")
    missing = [name for name in tool.required if arguments.get(name) in (None, "")]
    if missing:
        raise ToolError(f"Missing {', '.join(missing)}.")
    return {name: _coerce(value, tool.params[name], name) for name, value in arguments.items() if name in tool.params and value is not None}


# ---------------------------------------------------------------- Git tools
async def list_projects(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    return {"projects": await b.gitlab.list_project_paths()}


async def list_merge_requests(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    assignee = a.get("assignee") or (b.username(c) if a.get("assignedToMe") else None)
    if assignee and "@" in assignee:
        assignee = b.settings.user_map.get(assignee) or assignee.split("@")[0]
    items = await b.gitlab.list_merge_requests(path, state=a.get("state", "opened"), assignee=assignee)
    return {"project": path, "mergeRequests": [
        {"iid": mr["iid"], "title": mr["title"], "state": mr["state"], "sourceBranch": mr["source_branch"],
         "targetBranch": mr.get("target_branch", ""), "url": mr["web_url"]} for mr in items]}


async def create_branch(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    branch = a["branch"].strip()
    if not _BRANCH_NAME.match(branch):
        raise ToolError(f"{branch!r} is not a valid branch name.")
    path = await b.project(a.get("project", ""))
    created = await b.gitlab.create_branch(path, branch, a["ref"].strip())
    return {"project": path, "branch": branch, "ref": a["ref"], "url": created["web_url"]}


async def get_pipeline_status(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    pipeline = await b.gitlab.get_pipeline(path, a["pipelineId"])
    return {"project": path, "pipelineId": pipeline["id"], "status": pipeline["status"], "ref": pipeline["ref"],
            "coverage": pipeline.get("coverage"), "url": pipeline["web_url"]}


async def comment_on_merge_request(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    iid = a["mergeRequestIid"]
    await b.gitlab.comment_on_merge_request(path, iid, mask(a["body"]) + b.footer(c))
    merge_request = await b.gitlab.get_merge_request(path, iid)
    return {"project": path, "mergeRequestIid": iid, "url": merge_request["web_url"]}


async def create_issue(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    issue = await b.gitlab.create_issue(path, mask(a["title"])[:250], mask(a.get("description", "")) + b.footer(c))
    return {"project": path, "issueIid": issue["iid"], "url": issue["web_url"]}


# ---------------------------------------------------------------- test loop
def _session_view(session: TestSession) -> dict[str, Any]:
    steps = {
        "ready": "Write the test file and call SubmitTestFile.",
        "running": "CI is running. Call GetTestRun again (or the WaitForTestRun flow) until the status changes.",
        "failed": "Read failedLog. If the test is wrong, call SubmitTestFile with the corrected file. "
                  "If the code clearly contradicts its purpose, call ReportCodeBug instead. Never weaken a test to make it pass.",
        "exhausted": "Stop. Tell the user the tests still fail and are on the branch. Do not open a merge request.",
        "passed": "Call OpenTestMergeRequest.",
        "mr_opened": "Done. Show the summary and links.",
        "bug_reported": "Done. Show the summary and the pipeline link.",
    }
    view: dict[str, Any] = {
        "sessionId": session.id, "status": session.status, "project": session.project, "branch": session.branch,
        "sourceBranch": session.source_ref, "testPath": session.test_path, "attempt": session.attempt,
        "maxAttempts": session.max_attempts, "attemptsLeft": max(session.max_attempts - session.attempt, 0),
        "pipelineId": session.pipeline_id, "pipelineUrl": session.pipeline_url, "nextStep": steps[session.status],
    }
    if session.status in ("failed", "exhausted"):
        view["failedLog"] = session.failed_log
    if session.status in ("passed", "mr_opened"):
        view.update(passing=session.passing, failing=session.failing, coverageBefore=session.coverage_before, coverageAfter=session.coverage_after)
    if session.summary:
        view["summary"] = session.summary
    if session.merge_request_url:
        view["mergeRequestUrl"] = session.merge_request_url
    return view


def _ai_name(path: str) -> str:
    pure = PurePosixPath(path)
    return str(pure.with_name(f"{pure.stem}_ai{pure.suffix}"))


def _clip(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[:budget] + "\n...(truncated)"


async def start_test_session(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    symbol = a.get("symbol", "").strip().strip("`")
    tests, gitlab = b._tests, b.gitlab
    diff_text = ""
    if a.get("mergeRequestIid") is not None:
        source_ref, label, diff_text, files = await tests._gather_from_mr(path, a["mergeRequestIid"])
    else:
        source_ref = a.get("ref") or (await gitlab.get_project(path))["default_branch"]
        files = await tests._gather_from_symbol(path, source_ref, symbol)
        label = symbol or source_ref
    if not files:
        raise ToolError("I found no source files to test in that change.")

    budget = b.settings.max_input_chars
    sources = []
    for file_path in files[:8]:
        content = _clip(await gitlab.get_file(path, file_path, source_ref), budget // 8)
        sources.append({"path": file_path, "content": content})
    tree = await gitlab.list_tree(path, source_ref)
    examples = [p for p in tree if is_safe_test_path(p) and p.endswith(SOURCE_EXTENSIONS) and not p.endswith("__init__.py")]
    style = {"path": examples[0], "content": _clip(await gitlab.get_file(path, examples[0], source_ref), 6_000)} if examples else None
    rules = await gitlab.get_file(path, ".devassist/testing-rules.md", source_ref) if ".devassist/testing-rules.md" in tree else ""

    before = await tests._baseline_coverage(path, source_ref)
    branch = _unique_branch("tests", label)
    await gitlab.create_branch(path, branch, source_ref)
    session = b.sessions.create(project=path, source_ref=source_ref, branch=branch, label=label, created_by=c.email,
                                max_attempts=b.settings.max_test_attempts, coverage_before=before)
    return {**_session_view(session), "label": label, "focus": symbol, "fileFormat": _FILE_FORMAT, "testingRules": _clip(rules, 4_000),
            "diff": _clip(diff_text, budget // 2), "sourceFiles": sources, "styleExample": style, "coverageBefore": before}


async def submit_test_file(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    session = b.session(a["sessionId"], c, owner_only=True)
    if session.status in ("running", "passed") or session.status in FINISHED:
        raise ToolError(f"This session is {session.status}; it does not accept a new file. {_session_view(session)['nextStep']}")
    if session.attempt >= session.max_attempts:
        raise ToolError(f"All {session.max_attempts} attempts are used.")
    path = a["path"].strip().strip("`")
    if not is_safe_test_path(path) or not path.endswith(SOURCE_EXTENSIONS):
        raise ToolError(f"{path!r} is not a test file inside a tests folder, so nothing was committed.")
    fenced = _FENCE.match(a["content"].strip())
    content = (fenced.group(1) if fenced else a["content"]).rstrip() + "\n"
    if len(content) > b.settings.max_diff_chars:
        raise ToolError("The test file is larger than the configured limit.")

    gitlab = b.gitlab
    if not session.test_path:
        if await gitlab.file_exists(session.project, path, session.source_ref):  # never overwrite a test a person wrote
            path = _ai_name(path)
        session.test_path = path
    elif session.test_path not in (path, _ai_name(path)):
        raise ToolError(f"This session only edits {session.test_path}.")

    action = "create" if session.attempt == 0 else "update"
    session.attempt += 1
    await gitlab.commit_files(session.project, session.branch, f"test: AI-generated tests for {session.label} (attempt {session.attempt})",
                              {session.test_path: content}, {session.test_path: action})
    pipeline = await gitlab.trigger_pipeline(session.project, session.branch)
    session.content, session.status = content, "running"
    session.pipeline_id, session.pipeline_url, session.failed_log = pipeline["id"], pipeline["web_url"], ""
    b.sessions.save(session)
    return _session_view(session)


async def _refresh(b: Backend, session: TestSession) -> None:
    """Read the pipeline once and move the session on if it finished. CI decides, never the model."""
    pipeline = await b.gitlab.get_pipeline(session.project, session.pipeline_id)  # type: ignore[arg-type]
    if pipeline["status"] not in ("success", "failed", "canceled", "skipped", "manual"):
        return
    if pipeline["status"] == "success":
        session.passing, session.failing = await b._tests._test_counts(session.project, pipeline["id"])
        session.coverage_after = float(pipeline["coverage"]) if pipeline.get("coverage") is not None else None
        session.status = "passed"
    else:
        session.failed_log = _tail(await b._tests._failed_job_log(session.project, pipeline["id"]), 4_000)
        session.status = "exhausted" if session.attempt >= session.max_attempts else "failed"
        if session.status == "exhausted":
            session.summary = (f"The tests still fail after {session.attempt} attempts, so no merge request was opened. "
                               f"They are on `{session.branch}`.")
    b.sessions.save(session)


async def get_test_run(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    session = b.session(a["sessionId"], c)
    wait = min(a.get("waitSeconds", 0), MAX_WAIT_SECONDS)
    deadline = time.monotonic() + wait
    while session.status == "running":
        await _refresh(b, session)
        if session.status != "running" or time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(b.settings.pipeline_poll_seconds, max(deadline - time.monotonic(), 0)))
    return _session_view(session)


async def open_test_merge_request(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    session = b.session(a["sessionId"], c, owner_only=True)
    if session.status == "mr_opened":
        return _session_view(session)
    if session.status != "passed":
        raise ToolError(f"A merge request is only opened after CI passes; this session is {session.status}.")
    title = f"AI tests for {session.label}"[:200]
    description = f"Generated by DevAssist. Pipeline: {session.pipeline_url}\n\nPlease review before merging." + b.footer(c)
    merge_request = await b.gitlab.create_merge_request(session.project, session.branch, session.source_ref, title, description)
    coverage = ""
    if session.coverage_after is not None:
        before = f"{session.coverage_before:.0f}% → " if session.coverage_before is not None else ""
        coverage = f" Coverage {before}{session.coverage_after:.0f}%."
    added = len(_TEST_CASE.findall(session.content))
    session.summary = f"{added} tests added. Pipeline: {session.passing} passing, {session.failing} failing.{coverage}"
    session.merge_request_url, session.status = merge_request["web_url"], "mr_opened"
    b.sessions.save(session)
    return _session_view(session)


async def report_code_bug(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    session = b.session(a["sessionId"], c, owner_only=True)
    if session.status not in ("failed", "exhausted"):
        raise ToolError("A bug can only be reported after a red pipeline shows it.")
    session.summary = (f"I stopped because this looks like a bug in the code, not in the tests: {mask(a['explanation']).strip()} "
                       f"The failing tests are on `{session.branch}`.")
    session.status = "bug_reported"
    b.sessions.save(session)
    return _session_view(session)


# ---------------------------------------------------------------- docs
async def get_docs_sources(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    ref = (await b.gitlab.get_project(path))["default_branch"]
    tree = await b.gitlab.list_tree(path, ref)
    kind, symbol = a.get("kind", "api"), a.get("symbol", "")
    if kind == "comments":
        chosen = [p for p in tree if p.endswith(".py") and not is_safe_test_path(p) and not p.endswith("__init__.py")][:8]
        guidance = ("Add docstrings and comments only. Return each file complete in OpenCodeCommentsMergeRequest; "
                    "files where any code changed are rejected.")
    else:
        source = [p for p in tree if p.endswith(SOURCE_EXTENSIONS) and not is_safe_test_path(p) and not p.endswith("__init__.py")]
        chosen = ([p for p in source if _API_FILE.search(p)] or source)[:12]
        guidance = "Write one Markdown API reference (endpoints, parameters, responses) and pass it to OpenApiReferenceMergeRequest."
    files = []
    for file_path in chosen:
        content = await b.gitlab.get_file(path, file_path, ref)
        if symbol and symbol not in content:
            continue
        files.append({"path": file_path, "content": _clip(content, b.settings.max_input_chars // max(len(chosen), 1))})
    if not files:
        raise ToolError("I found no source files to document.")
    return {"project": path, "ref": ref, "kind": kind, "files": files, "guidance": guidance}


async def open_api_reference_mr(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    document = a["markdown"].strip()
    fenced = _FENCE.match(document)
    document = (fenced.group(1) if fenced else document).rstrip() + "\n"
    if len(document) > MAX_DOC_CHARS:
        raise ToolError("The document is too large.")
    ref = (await b.gitlab.get_project(path))["default_branch"]
    files = {"docs/api.md": document}
    if "README.md" in await b.gitlab.list_tree(path, ref):
        readme = await b.gitlab.get_file(path, "README.md", ref)
        if "docs/api.md" not in readme:
            files["README.md"] = readme.rstrip() + "\n\n## API\n\nSee the [API reference](docs/api.md).\n"
    merge_request = await b._docs._open_mr(path, "docs", ref, f"docs: API reference for {path.rsplit('/', 1)[-1]}", files)
    summary = "API reference written to `docs/api.md`" + (" and linked from the README." if "README.md" in files else ".")
    return {"project": path, "summary": summary, "mergeRequestUrl": merge_request["web_url"]}


async def open_code_comments_mr(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    ref = (await b.gitlab.get_project(path))["default_branch"]
    allowed = {p for p in await b.gitlab.list_tree(path, ref) if p.endswith(".py") and not is_safe_test_path(p)}
    changed: dict[str, str] = {}
    skipped: list[str] = []
    for item in a["files"]:
        file_path, updated = str(item.get("path", "")), str(item.get("content", ""))
        if file_path not in allowed:
            skipped.append(file_path)
            continue
        fenced = _FENCE.match(updated.strip())
        updated = (fenced.group(1) if fenced else updated).rstrip() + "\n"
        original = await b.gitlab.get_file(path, file_path, ref)
        try:
            same_code = _code_without_docs(original) == _code_without_docs(updated)
        except SyntaxError:
            same_code = False
        if not same_code:
            skipped.append(file_path)
        elif updated != original:
            changed[file_path] = updated
    if not changed:
        raise ToolError("No file could be commented without changing code" + (f" (rejected: {', '.join(skipped)})." if skipped else "."))
    merge_request = await b._docs._open_mr(path, "comments", ref, "docs: add code comments", changed)
    note = f" Rejected because code changed or the file is not allowed: {', '.join(skipped)}." if skipped else ""
    return {"project": path, "summary": f"Added documentation comments to {len(changed)} file(s).{note}",
            "changed": sorted(changed), "rejected": skipped, "mergeRequestUrl": merge_request["web_url"]}


async def open_changelog_mr(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    reply = await b._docs.changelog(path)
    return {"project": path, "summary": reply.text, "mergeRequestUrl": reply.links[0][1]}


# ---------------------------------------------------------------- traces
async def investigate_incident(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    question = a["question"]
    start, end = parse_time_range(question, tz_name=b.settings.timezone)
    known = await b.logs.services()
    service = a.get("service") or next((name for name in known if name.lower() in question.lower()), None)
    if service and service not in known:
        raise ToolError(f"Unknown service {service!r}. Known: {', '.join(known)}.")
    entries = await b.logs.query(start, end, service)
    groups = group_errors(entries)
    deployments: list[dict[str, Any]] = []
    project = a.get("project", "")
    if project:
        path = await b.project(project)
        for item in await b.gitlab.list_deployments(path):
            when = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            if start.astimezone(timezone.utc).timestamp() - 7200 <= when.timestamp() <= end.astimezone(timezone.utc).timestamp():
                deployments.append({"createdAt": item["created_at"], "environment": item.get("environment", {}).get("name", ""),
                                    "ref": item.get("ref", ""), "sha": item.get("sha", "")[:8]})
    tz = b.settings.timezone
    return {
        "service": service or "all services", "window": f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} {tz}",
        "linesChecked": len(entries), "totalErrors": sum(g.count for g in groups), "groupCount": len(groups),
        "groups": [{"count": g.count, "firstSeen": f"{g.first_seen:%H:%M:%S}", "lastSeen": f"{g.last_seen:%H:%M:%S}",
                    "sample": g.sample[:300]} for g in groups[:10]],
        "deployments": deployments, "complex": len(groups) > 3,
    }


async def get_failed_pipeline_log(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    path = await b.project(a.get("project", ""))
    pipeline_id = a["pipelineId"]
    pipeline = await b.gitlab.get_pipeline(path, pipeline_id)
    result: dict[str, Any] = {"project": path, "pipelineId": pipeline_id, "status": pipeline["status"], "url": pipeline["web_url"]}
    if pipeline["status"] != "failed":
        return {**result, "note": f"Pipeline #{pipeline_id} is not red: its status is {pipeline['status']}."}
    failed = [job for job in await b.gitlab.list_pipeline_jobs(path, pipeline_id) if job["status"] == "failed"]
    if not failed:
        return {**result, "note": "The pipeline failed, but no job is marked as failed."}
    job = failed[0]
    log = mask(_tail(await b.gitlab.get_job_log(path, job["id"])))
    return {**result, "job": {"name": job["name"], "stage": job["stage"], "url": job["web_url"]}, "log": log}


async def record_feedback(b: Backend, a: dict[str, Any], c: Caller) -> dict[str, Any]:
    b.audit.event("feedback", rating=a["rating"], comment=mask(a.get("comment", ""))[:500])
    return {"recorded": True}


# ---------------------------------------------------------------- registry
TOOLS: tuple[Tool, ...] = (
    Tool("ListProjects", "List enabled GitLab projects", "List the GitLab projects DevAssist may work on.",
         {}, (), {"projects": {"type": "array", "items": {"type": "string"}}}, True, list_projects),
    Tool("ListMergeRequests", "List merge requests", "List merge requests of a project, optionally only those assigned to the person asking.",
         {"project": PROJECT, "state": _s("opened, merged, closed or all", enum=["opened", "merged", "closed", "all"]),
          "assignedToMe": _b("Only merge requests assigned to the person asking"), "assignee": _s("GitLab username or e-mail of the assignee")},
         ("project",), {"project": _s("Project path"), "mergeRequests": _obj_list("Merge requests", {
             "iid": _i("Number"), "title": _s("Title"), "state": _s("State"), "sourceBranch": _s("Source branch"),
             "targetBranch": _s("Target branch"), "url": LINK})}, True, list_merge_requests),
    Tool("CreateBranch", "Create a branch", "Create a branch from an existing branch. Confirm the name with the user first.",
         {"project": PROJECT, "branch": _s("Name of the new branch"), "ref": _s("Existing branch to create it from")},
         ("project", "branch", "ref"), {"project": _s("Project path"), "branch": _s("Branch"), "ref": _s("From"), "url": LINK},
         False, create_branch),
    Tool("GetPipelineStatus", "Get pipeline status", "Get the status, branch and coverage of one pipeline.",
         {"project": PROJECT, "pipelineId": _i("Pipeline number", minimum=1)}, ("project", "pipelineId"),
         {"project": _s("Project path"), "pipelineId": _i("Pipeline"), "status": _s("Status"), "ref": _s("Branch"),
          "coverage": _s("Coverage percent reported by CI"), "url": LINK}, True, get_pipeline_status),
    Tool("CommentOnMergeRequest", "Comment on a merge request", "Add a comment to a merge request. Personal data is masked. Confirm the text with the user first.",
         {"project": PROJECT, "mergeRequestIid": _i("Merge request number", minimum=1), "body": _s("Comment in Markdown")},
         ("project", "mergeRequestIid", "body"), {"project": _s("Project path"), "mergeRequestIid": _i("Merge request"), "url": LINK},
         False, comment_on_merge_request),
    Tool("CreateIssue", "Create an issue", "Create a GitLab issue, for example from a diagnosis. Personal data is masked. Confirm with the user first.",
         {"project": PROJECT, "title": _s("Issue title"), "description": _s("Issue description in Markdown")},
         ("project", "title"), {"project": _s("Project path"), "issueIid": _i("Issue number"), "url": LINK}, False, create_issue),
    Tool("StartTestSession", "Start writing tests",
         "Step 1 of writing unit tests. Reads a merge request (or a class on a branch), creates an ai/tests-* branch and returns "
         "the diff, source files, a style example and the team's testing rules. Then write the tests and call SubmitTestFile.",
         {"project": PROJECT, "mergeRequestIid": _i("Merge request number, if the tests are for an MR", minimum=1),
          "ref": _s("Branch, when there is no merge request"), "symbol": _s("Class or function to focus on, e.g. OrderService")},
         ("project",), {"sessionId": _s("Session id for the next calls"), "status": _s("Session status"), "branch": _s("ai/tests-* branch"),
                        "diff": _s("Merge request diff"), "sourceFiles": _obj_list("Source files", FILES), "fileFormat": _s("How to submit"),
                        "testingRules": _s("Team testing rules"), "nextStep": _s("What to do next")}, False, start_test_session),
    Tool("SubmitTestFile", "Commit tests and run CI",
         "Step 2. Commits the complete test file to the session's ai/tests-* branch and starts the GitLab pipeline. "
         "Only files inside a tests folder are accepted. Limited attempts per session.",
         {"sessionId": _s("Session id from StartTestSession"), "path": _s("Test file path, inside the tests folder"),
          "content": _s("Complete content of the test file")}, ("sessionId", "path", "content"),
         {"sessionId": _s("Session id"), "status": _s("running"), "attempt": _i("Attempt number"), "attemptsLeft": _i("Attempts left"),
          "pipelineId": _i("Pipeline"), "pipelineUrl": LINK, "nextStep": _s("What to do next")}, False, submit_test_file),
    Tool("GetTestRun", "Get the test run result",
         "Step 3. Reads the result of the session's pipeline from GitLab: running, passed, failed (with the masked job log) "
         "or exhausted. Can wait up to 25 seconds. The pass/fail result and counts come from CI, never from you.",
         {"sessionId": _s("Session id"), "waitSeconds": _i("Seconds to wait for CI to finish, 0 to 25", minimum=0, maximum=MAX_WAIT_SECONDS)},
         ("sessionId",), {"sessionId": _s("Session id"), "status": _s("ready, running, failed, exhausted, passed, mr_opened or bug_reported"),
                          "attempt": _i("Attempt"), "attemptsLeft": _i("Attempts left"), "failedLog": _s("Masked log of the failed job"),
                          "passing": _i("Tests passing"), "failing": _i("Tests failing"), "pipelineUrl": LINK,
                          "summary": _s("Final result, when finished"), "nextStep": _s("What to do next")}, True, get_test_run),
    Tool("OpenTestMergeRequest", "Open the tests merge request",
         "Step 4. Opens a merge request with the tests, only if the last pipeline passed. Returns the summary to show.",
         {"sessionId": _s("Session id")}, ("sessionId",),
         {"status": _s("mr_opened"), "summary": SUMMARY, "mergeRequestUrl": LINK, "pipelineUrl": LINK}, False, open_test_merge_request),
    Tool("ReportCodeBug", "Stop: the code has a bug",
         "Use instead of another attempt when the failing test is right and the code clearly contradicts its purpose. "
         "Closes the session without a merge request.",
         {"sessionId": _s("Session id"), "explanation": _s("The bug and where it is, in one or two sentences")},
         ("sessionId", "explanation"), {"status": _s("bug_reported"), "summary": SUMMARY, "pipelineUrl": LINK}, False, report_code_bug),
    Tool("GetDocsSources", "Read code to document",
         "Returns the controller/route files (kind=api) or Python files (kind=comments) of a project's default branch, for writing documentation.",
         {"project": PROJECT, "kind": _s("api or comments", enum=["api", "comments"]), "symbol": _s("Only files that mention this name")},
         ("project",), {"project": _s("Project path"), "ref": _s("Branch"), "files": _obj_list("Files", FILES), "guidance": _s("What to write")},
         True, get_docs_sources),
    Tool("OpenApiReferenceMergeRequest", "Open an MR with docs/api.md",
         "Writes the Markdown you wrote to docs/api.md, links it from the README and opens a merge request.",
         {"project": PROJECT, "markdown": _s("Complete API reference in Markdown")}, ("project", "markdown"),
         {"project": _s("Project path"), "summary": SUMMARY, "mergeRequestUrl": LINK}, False, open_api_reference_mr),
    Tool("OpenCodeCommentsMergeRequest", "Open an MR with code comments",
         "Commits Python files where you only added docstrings or comments, and opens a merge request. "
         "The backend compares syntax trees and rejects any file where code changed.",
         {"project": PROJECT, "files": _obj_list("Complete updated files", FILES)}, ("project", "files"),
         {"project": _s("Project path"), "summary": SUMMARY, "mergeRequestUrl": LINK}, False, open_code_comments_mr),
    Tool("OpenChangelogMergeRequest", "Open an MR updating CHANGELOG.md",
         "Builds the changelog from merged merge requests (by code, so nothing is invented) and opens a merge request.",
         {"project": PROJECT}, ("project",), {"project": _s("Project path"), "summary": SUMMARY, "mergeRequestUrl": LINK},
         False, open_changelog_mr),
    Tool("InvestigateIncident", "Look up errors in the logs",
         "For questions like 'why did checkout fail yesterday between 17:00 and 18:00'. Reads the logs for that time range "
         "(read-only, personal data masked), groups and counts the errors in code, and lists deployments near that time. "
         "Diagnose from this evidence and never change the counts.",
         {"question": _s("The user's question, including the time range"), "service": _s("Service name, if known"),
          "project": _s("GitLab project, to check recent deployments")}, ("question",),
         {"service": _s("Service"), "window": _s("Time window"), "totalErrors": _i("Errors"), "groupCount": _i("Groups"),
          "groups": _obj_list("Error groups", {"count": _i("Count"), "firstSeen": _s("First"), "lastSeen": _s("Last"), "sample": _s("Sample")}),
          "complex": _b("More than 3 error groups")}, True, investigate_incident),
    Tool("GetFailedPipelineLog", "Read a red pipeline's log",
         "For 'why is pipeline #N red'. Returns the failing job and the end of its log, with personal data masked.",
         {"project": PROJECT, "pipelineId": _i("Pipeline number", minimum=1)}, ("project", "pipelineId"),
         {"status": _s("Pipeline status"), "url": LINK, "job": {"type": "object", "properties": {"name": _s("Job"), "stage": _s("Stage"), "url": LINK}},
          "log": _s("End of the masked job log"), "note": _s("Set when the pipeline is not red")}, True, get_failed_pipeline_log),
    Tool("RecordFeedback", "Record feedback", "Record the user's feedback about DevAssist when they give it.",
         {"rating": _s("good or bad", enum=["good", "bad"]), "comment": _s("What they said")}, ("rating",),
         {"recorded": _b("Saved")}, False, record_feedback),
)
TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
