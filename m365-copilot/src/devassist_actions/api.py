"""DevAssist actions API: the tools Microsoft 365 Copilot calls for the declarative agent.

Copilot's own orchestrator and model do the reasoning (which specialist, what to
write, what the logs mean). This API only does what must be code: GitLab calls
through the allow-listed client, counting errors, masking personal data, running
the CI loop and opening MRs when tests are green. Every operation here matches an
operationId in appPackage/apiSpecificationFile/devassist-actions.yml.

Each call answers in well under a second or two, because a plugin call has a
short time budget. Anything slower (the test loop) is a job with a status call.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from aiohttp import web

from devassist.audit import AuditLog, RequestContext, current_request
from devassist.config import Settings
from devassist.gitlab import GitLabClient, GitLabError
from devassist.logstore import LogStore, group_errors, make_log_store, parse_time_range
from devassist.masking import mask
from devassist.specialists import SOURCE_EXTENSIONS, AgentRefusal, DocsAgent, _code_without_docs, _tail, is_safe_test_path
from devassist_actions.auth import AuthError, Authenticator, Caller
from devassist_actions.test_runs import RunRefusal, RunStore, TestRunner

logger = logging.getLogger("devassist_actions.api")
MAX_DOC_CHARS = 100_000
_STATES = ("opened", "merged", "closed", "all")

CALLER = getattr(web, "RequestKey", web.AppKey)("caller", Caller)  # RequestKey needs aiohttp 3.12+
RUNNER = web.AppKey("runner", TestRunner)
GITLAB = web.AppKey("gitlab", GitLabClient)


class BadRequest(Exception):
    pass


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit] + "\n…(truncated)", True)


def _int(value: Any, name: str, required: bool = True) -> int | None:
    if value in (None, ""):
        if required:
            raise BadRequest(f"{name} is required.")
        return None
    try:
        return int(str(value).lstrip("!#"))
    except ValueError as exc:
        raise BadRequest(f"{name} must be a number, got {value!r}.") from exc


def _text(source: dict[str, Any], name: str, required: bool = True) -> str:
    value = source.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BadRequest(f"{name} is required.")
        return ""
    if not isinstance(value, (str, int, float)):
        raise BadRequest(f"{name} must be text.")
    return str(value)


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _gitlab_status(exc: GitLabError) -> int:
    text = str(exc)
    if text.startswith("Blocked"):
        return 403
    if "not enabled" in text or "ambiguous" in text or "not found" in text or " 404" in text:
        return 404 if "not found" in text or " 404" in text else 400
    return 502


def create_app(settings: Settings, authenticator: Authenticator, *, gitlab: GitLabClient | None = None,
               logs: LogStore | None = None, runs_path: Path | None = None, max_response_chars: int | None = None) -> web.Application:
    audit = AuditLog(settings.audit_path)
    gitlab = gitlab or GitLabClient(settings, audit)
    logs = logs or make_log_store(settings)
    store = RunStore(runs_path or settings.audit_path.with_name("test-runs.json"))
    runner = TestRunner(gitlab, settings, store)
    docs = DocsAgent(llm=None, gitlab=gitlab, settings=settings)  # type: ignore[arg-type]  # only its model-free helpers are used
    budget = max_response_chars or int(os.environ.get("DEVASSIST_ACTIONS_MAX_RESPONSE_CHARS", "20000"))

    def username(caller: Caller) -> str:
        if caller.email and caller.email in settings.user_map:
            return settings.user_map[caller.email]
        return caller.email.split("@")[0] if caller.email else caller.object_id

    @web.middleware
    async def guard(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if not request.path.startswith("/api/"):
            return await handler(request)
        started = time.perf_counter()
        operation = getattr(request.match_info.route, "name", None) or request.path
        try:
            caller = await authenticator.authenticate(request.headers.get("Authorization"))
        except AuthError as exc:
            audit.event("action_denied", operation=operation, status=exc.status, reason=str(exc))
            return web.json_response({"error": str(exc)}, status=exc.status)
        request[CALLER] = caller
        token = current_request.set(RequestContext(user=caller.name, email=caller.email, conversation_id="m365-copilot", source="m365-copilot"))
        status = 500
        try:
            response = await handler(request)
            status = response.status
            return response
        except BadRequest as exc:
            status = 400
            return web.json_response({"error": str(exc)}, status=400)
        except RunRefusal as exc:
            status = exc.status
            return web.json_response({"error": str(exc)}, status=exc.status)
        except AgentRefusal as exc:
            status = 400
            return web.json_response({"error": str(exc)}, status=400)
        except GitLabError as exc:
            status = _gitlab_status(exc)
            return web.json_response({"error": str(exc)[:500]}, status=status)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("Action %s failed", operation)
            return web.json_response({"error": "Unexpected error in the DevAssist actions API."}, status=500)
        finally:
            audit.event("action", operation=operation, method=request.method, status=status,
                        ms=round((time.perf_counter() - started) * 1000))
            current_request.reset(token)

    async def body(request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError as exc:
            raise BadRequest("The body must be JSON.") from exc
        if not isinstance(data, dict):
            raise BadRequest("The body must be a JSON object.")
        return data

    async def project_of(name: str) -> str:
        return await gitlab.resolve_project(name)

    # ---------------------------------------------------------------- Git agent
    async def list_projects(request: web.Request) -> web.Response:
        return web.json_response({"projects": await gitlab.list_project_paths()})

    async def list_merge_requests(request: web.Request) -> web.Response:
        q = request.query
        project = await project_of(_text(q, "project"))
        state = q.get("state") or "opened"
        if state not in _STATES:
            raise BadRequest(f"state must be one of {', '.join(_STATES)}.")
        assignee = username(request[CALLER]) if _bool(q.get("assignedToMe")) else None
        items = await gitlab.list_merge_requests(project, state=state, assignee=assignee)
        return web.json_response({"project": project, "assignee": assignee, "merge_requests": [
            {"iid": mr["iid"], "title": mr["title"], "state": mr["state"], "source_branch": mr["source_branch"],
             "target_branch": mr.get("target_branch", ""), "web_url": mr["web_url"]} for mr in items]})

    async def create_branch(request: web.Request) -> web.Response:
        data = await body(request)
        project = await project_of(_text(data, "project"))
        branch, ref = _text(data, "branch").strip(), _text(data, "ref").strip()
        created = await gitlab.create_branch(project, branch, ref)
        return web.json_response({"project": project, "branch": branch, "from": ref, "web_url": created.get("web_url", "")}, status=201)

    async def pipeline_status(request: web.Request) -> web.Response:
        project = await project_of(_text(request.query, "project"))
        pipeline = await gitlab.get_pipeline(project, _int(request.query.get("pipelineId"), "pipelineId"))  # type: ignore[arg-type]
        return web.json_response({"project": project, "id": pipeline["id"], "status": pipeline["status"], "ref": pipeline.get("ref", ""),
                                  "coverage": pipeline.get("coverage"), "web_url": pipeline["web_url"]})

    async def comment_on_merge_request(request: web.Request) -> web.Response:
        data = await body(request)
        project = await project_of(_text(data, "project"))
        iid = _int(data.get("iid"), "iid")
        await gitlab.comment_on_merge_request(project, iid, _text(data, "body"))  # type: ignore[arg-type]
        merge_request = await gitlab.get_merge_request(project, iid)  # type: ignore[arg-type]
        return web.json_response({"project": project, "commented_on": iid, "web_url": merge_request["web_url"]}, status=201)

    async def create_issue(request: web.Request) -> web.Response:
        data = await body(request)
        project = await project_of(_text(data, "project"))
        issue = await gitlab.create_issue(project, _text(data, "title")[:250], _text(data, "description", required=False))
        return web.json_response({"project": project, "iid": issue["iid"], "web_url": issue["web_url"]}, status=201)

    # ---------------------------------------------------------- reading code
    async def merge_request_changes(request: web.Request) -> web.Response:
        q = request.query
        project = await project_of(_text(q, "project"))
        iid = _int(q.get("iid"), "iid")
        merge_request = await gitlab.get_merge_request(project, iid)  # type: ignore[arg-type]
        diffs = await gitlab.get_merge_request_diffs(project, iid)  # type: ignore[arg-type]
        diff_text = "\n".join(f"--- {d['new_path']}\n{d['diff']}" for d in diffs)
        if len(diff_text) > settings.max_diff_chars:
            raise AgentRefusal(f"MR !{iid} has a {len(diff_text):,} character diff, over the {settings.max_diff_chars:,} limit. "
                               "Ask about one class or file instead, and read it with getFileContent.")
        ref = merge_request["source_branch"]
        changed = [d["new_path"] for d in diffs if not d.get("deleted_file") and d["new_path"].endswith(SOURCE_EXTENSIONS) and not is_safe_test_path(d["new_path"])]
        remaining = budget
        diff_out, diff_cut = _clip(diff_text, remaining // 2)
        remaining -= len(diff_out)
        sources = []
        for path in changed[:8]:
            content, cut = _clip(await gitlab.get_file(project, path, ref), max(remaining, 0))
            remaining -= len(content)
            sources.append({"path": path, "content": content, "truncated": cut})
        tree = await gitlab.list_tree(project, ref)
        examples = [p for p in tree if is_safe_test_path(p) and p.endswith(SOURCE_EXTENSIONS) and not p.endswith("__init__.py")]
        example = {"path": examples[0], "content": _clip(await gitlab.get_file(project, examples[0], ref), 3_000)[0]} if examples else {"path": "", "content": ""}
        rules = _clip(await gitlab.get_file(project, ".devassist/testing-rules.md", ref), 3_000)[0] if ".devassist/testing-rules.md" in tree else ""
        return web.json_response({
            "project": project, "iid": iid, "title": merge_request["title"], "source_branch": ref,
            "target_branch": merge_request.get("target_branch", ""), "web_url": merge_request["web_url"],
            "diff": diff_out, "diff_truncated": diff_cut, "changed_source_files": sources,
            "example_test_path": example["path"], "example_test": example["content"], "testing_rules": rules,
        })

    async def list_files(request: web.Request) -> web.Response:
        q = request.query
        project = await project_of(_text(q, "project"))
        ref = q.get("ref") or (await gitlab.get_project(project))["default_branch"]
        needle = (q.get("pathContains") or "").lower()
        paths = [p for p in await gitlab.list_tree(project, ref) if needle in p.lower()]
        return web.json_response({"project": project, "ref": ref, "files": paths[:300], "truncated": len(paths) > 300})

    async def file_content(request: web.Request) -> web.Response:
        q = request.query
        project = await project_of(_text(q, "project"))
        ref = q.get("ref") or (await gitlab.get_project(project))["default_branch"]
        path = _text(q, "path")
        content, cut = _clip(await gitlab.get_file(project, path, ref), budget)
        return web.json_response({"project": project, "ref": ref, "path": path, "content": content, "truncated": cut})

    # ---------------------------------------------------------- test agent
    async def start_test_run(request: web.Request) -> web.Response:
        data = await body(request)
        caller = request[CALLER]
        project = await project_of(_text(data, "project"))
        mr_iid = _int(data.get("mergeRequestIid"), "mergeRequestIid", required=False)
        run = await runner.start(caller.object_id, caller.name, project, _text(data, "testFilePath").strip(), _text(data, "testFileContent"),
                                 mr_iid=mr_iid, ref=_text(data, "ref", required=False) or None, label=_text(data, "label", required=False))
        return web.json_response(run.public(), status=202)

    async def get_test_run(request: web.Request) -> web.Response:
        return web.json_response(runner.get(request.match_info["runId"], request[CALLER].object_id).public())

    async def retry_test_run(request: web.Request) -> web.Response:
        data = await body(request)
        run = await runner.retry(request.match_info["runId"], request[CALLER].object_id, _text(data, "testFileContent"))
        return web.json_response(run.public(), status=202)

    async def report_bug(request: web.Request) -> web.Response:
        data = await body(request)
        run = await runner.report_bug(request.match_info["runId"], request[CALLER].object_id, _text(data, "summary"))
        return web.json_response(run.public())

    # ---------------------------------------------------------- docs agent
    async def propose_api_reference(request: web.Request) -> web.Response:
        data = await body(request)
        project = await project_of(_text(data, "project"))
        document = _text(data, "content")
        if len(document) > MAX_DOC_CHARS:
            raise BadRequest(f"The document has {len(document):,} characters, over the {MAX_DOC_CHARS:,} limit.")
        ref = (await gitlab.get_project(project))["default_branch"]
        files = {"docs/api.md": document.rstrip() + "\n"}
        if await gitlab.file_exists(project, "README.md", ref):
            readme = await gitlab.get_file(project, "README.md", ref)
            if "docs/api.md" not in readme:
                files["README.md"] = readme.rstrip() + "\n\n## API\n\nSee the [API reference](docs/api.md).\n"
        merge_request = await docs._open_mr(project, "docs", ref, f"docs: API reference for {project.rsplit('/', 1)[-1]}", files)
        return web.json_response({"project": project, "files": sorted(files), "merge_request_url": merge_request["web_url"]}, status=201)

    async def propose_changelog(request: web.Request) -> web.Response:
        data = await body(request)
        project = await project_of(_text(data, "project"))
        reply = await docs.changelog(project)  # built from merged MRs by code alone
        return web.json_response({"project": project, "summary": reply.text, "merge_request_url": reply.links[0][1]}, status=201)

    async def propose_code_comments(request: web.Request) -> web.Response:
        data = await body(request)
        project = await project_of(_text(data, "project"))
        path, updated = _text(data, "path").strip(), _text(data, "content")
        pure = PurePosixPath(path)
        if pure.suffix != ".py" or is_safe_test_path(path) or ".." in pure.parts:
            raise BadRequest("Only Python source files (not tests) can be commented through DevAssist.")
        ref = (await gitlab.get_project(project))["default_branch"]
        original = await gitlab.get_file(project, path, ref)
        updated = updated.rstrip() + "\n"
        try:
            same_code = _code_without_docs(original) == _code_without_docs(updated)
        except SyntaxError:
            same_code = False
        if not same_code:
            raise BadRequest(f"Rejected: the new {path} changes code, not only docstrings. Only add or edit docstrings and comments.")
        if updated == original:
            raise BadRequest("The file is unchanged.")
        merge_request = await docs._open_mr(project, "comments", ref, f"docs: add code comments to {pure.name}", {path: updated})
        return web.json_response({"project": project, "path": path, "merge_request_url": merge_request["web_url"]}, status=201)

    # ---------------------------------------------------------- trace agent
    async def search_incident_logs(request: web.Request) -> web.Response:
        q = request.query
        question = _text(q, "question")
        start, end = parse_time_range(question, tz_name=settings.timezone)
        known = await logs.services()
        service = q.get("service") or next((name for name in known if name.lower() in question.lower()), None)
        if service and service not in known:
            raise BadRequest(f"Unknown service {service!r}. Known services: {', '.join(known)}.")
        entries = await logs.query(start, end, service)
        groups = group_errors(entries)
        deployments: list[dict[str, str]] = []
        project_name = q.get("project")
        if project_name:
            project = await project_of(project_name)
            for item in await gitlab.list_deployments(project):
                when = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                if start.astimezone(timezone.utc).timestamp() - 7200 <= when.timestamp() <= end.astimezone(timezone.utc).timestamp():
                    deployments.append({"created_at": item["created_at"], "environment": item.get("environment", {}).get("name", ""),
                                        "ref": item.get("ref", ""), "sha": item.get("sha", "")[:8]})
        return web.json_response({
            "service": service or "all services", "window_start": start.isoformat(), "window_end": end.isoformat(), "timezone": settings.timezone,
            "log_lines_checked": len(entries), "total_errors": sum(g.count for g in groups),
            "error_groups": [{"count": g.count, "first_seen": g.first_seen.isoformat(), "last_seen": g.last_seen.isoformat(), "sample": mask(g.sample)[:400]}
                             for g in groups[:10]],
            "deployments_in_window": deployments,
            "note": "Counts are measured by code and samples are masked. Diagnose from these facts only.",
        })

    async def pipeline_failure(request: web.Request) -> web.Response:
        project = await project_of(_text(request.query, "project"))
        pipeline_id = _int(request.query.get("pipelineId"), "pipelineId")
        pipeline = await gitlab.get_pipeline(project, pipeline_id)  # type: ignore[arg-type]
        result: dict[str, Any] = {"project": project, "id": pipeline_id, "status": pipeline["status"], "web_url": pipeline["web_url"]}
        if pipeline["status"] != "failed":
            return web.json_response({**result, "note": "The pipeline is not red."})
        failed = [job for job in await gitlab.list_pipeline_jobs(project, pipeline_id) if job["status"] == "failed"]  # type: ignore[arg-type]
        if not failed:
            return web.json_response({**result, "note": "The pipeline failed but no job is marked as failed."})
        job = failed[0]
        log = mask(_tail(await gitlab.get_job_log(project, job["id"]), min(budget, 8_000)))
        return web.json_response({**result, "job_name": job["name"], "stage": job["stage"], "job_url": job["web_url"], "log_tail": log})

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "authenticated": authenticator.config.enabled})

    async def on_startup(_: web.Application) -> None:
        resumed = runner.resume()
        if resumed:
            logger.info("Resumed %d test run(s) waiting for CI", resumed)

    async def on_cleanup(_: web.Application) -> None:
        await runner.drain()
        await gitlab.aclose()

    app = web.Application(middlewares=[guard])
    app[RUNNER] = runner
    app[GITLAB] = gitlab
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.get("/healthz", health),
        web.get("/api/projects", list_projects, name="listProjects"),
        web.get("/api/merge-requests", list_merge_requests, name="listMergeRequests"),
        web.get("/api/merge-requests/changes", merge_request_changes, name="getMergeRequestChanges"),
        web.post("/api/merge-requests/comments", comment_on_merge_request, name="commentOnMergeRequest"),
        web.get("/api/files", list_files, name="listFiles"),
        web.get("/api/files/content", file_content, name="getFileContent"),
        web.post("/api/branches", create_branch, name="createBranch"),
        web.get("/api/pipelines/status", pipeline_status, name="getPipelineStatus"),
        web.get("/api/pipelines/failure", pipeline_failure, name="getPipelineFailure"),
        web.post("/api/issues", create_issue, name="createIssue"),
        web.post("/api/test-runs", start_test_run, name="startTestRun"),
        web.get("/api/test-runs/{runId}", get_test_run, name="getTestRun"),
        web.post("/api/test-runs/{runId}/attempts", retry_test_run, name="retryTestRun"),
        web.post("/api/test-runs/{runId}/bug", report_bug, name="reportBugInCode"),
        web.post("/api/docs/api-reference", propose_api_reference, name="proposeApiReference"),
        web.post("/api/docs/changelog", propose_changelog, name="proposeChangelog"),
        web.post("/api/docs/code-comments", propose_code_comments, name="proposeCodeComments"),
        web.get("/api/incidents", search_incident_logs, name="searchIncidentLogs"),
    ])
    return app
