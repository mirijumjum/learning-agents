"""GitLab REST client, restricted on purpose.

Agents must never merge, deploy or delete, so those methods do not exist here,
and every write is checked against an allow-list before it leaves the process.
This holds even if a model is tricked, and sits on top of the bot account's own
GitLab permissions.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote

import httpx

from devassist.audit import AuditLog
from devassist.config import Settings

AI_BRANCH_PREFIX = "ai/"
_PROJECT = r"^/projects/[^/]+"
_ALLOWED_WRITES = [re.compile(_PROJECT + suffix + "$") for suffix in (
    r"/repository/branches",
    r"/repository/commits",
    r"/pipeline",
    r"/merge_requests",
    r"/merge_requests/\d+/notes",
    r"/issues",
)]
_FINISHED = {"success", "failed", "canceled", "skipped", "manual"}


class GitLabError(RuntimeError):
    pass


class GitLabClient:
    def __init__(self, settings: Settings, audit: AuditLog, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.audit = audit
        self._http = httpx.AsyncClient(
            base_url=f"{settings.gitlab_url}/api/v4",
            headers={"PRIVATE-TOKEN": settings.gitlab_token},
            timeout=60.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if method != "GET" and not any(rule.match(path) for rule in _ALLOWED_WRITES):
            self.audit.event("tool_blocked", tool="gitlab", method=method, path=path)
            raise GitLabError(f"Blocked: {method} {path} is not an allowed GitLab operation.")
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            self.audit.event("tool", tool="gitlab", method=method, path=path, error=type(exc).__name__)
            raise GitLabError(f"Could not reach GitLab at {self.settings.gitlab_url}: {exc}") from exc
        self.audit.event("tool", tool="gitlab", method=method, path=path, status=response.status_code)
        if response.status_code >= 400:
            raise GitLabError(f"GitLab {method} {path} returned {response.status_code}: {response.text[:300]}")
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return (await self._request(method, path, **kwargs)).json()

    def _pid(self, project: str) -> str:
        if self.settings.gitlab_projects and project not in self.settings.gitlab_projects:
            raise GitLabError(f"Project {project!r} is not enabled for DevAssist.")
        return quote(project, safe="")

    async def resolve_project(self, name: str) -> str:
        """Turn 'payments-api' or 'acme/payments-api' into the full path."""
        name = name.strip().strip("`'\"")
        allowed = self.settings.gitlab_projects
        if allowed:
            matches = [p for p in allowed if p == name or p.endswith("/" + name)]
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise GitLabError(f"{name!r} is ambiguous: {', '.join(matches)}.")
            raise GitLabError(f"Project {name!r} is not enabled for DevAssist. Enabled: {', '.join(allowed)}.")
        found = await self._json("GET", "/projects", params={"search": name.rsplit("/", 1)[-1], "simple": "true", "per_page": 50})
        matches = [p["path_with_namespace"] for p in found if p["path_with_namespace"] == name or p["path"] == name]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise GitLabError(f"Project {name!r} not found.")
        raise GitLabError(f"{name!r} is ambiguous: {', '.join(matches)}.")

    async def list_project_paths(self) -> list[str]:
        if self.settings.gitlab_projects:
            return list(self.settings.gitlab_projects)
        found = await self._json("GET", "/projects", params={"simple": "true", "per_page": 100})
        return [p["path_with_namespace"] for p in found]

    # ---- read
    async def get_project(self, project: str) -> dict:
        return await self._json("GET", f"/projects/{self._pid(project)}")

    async def get_merge_request(self, project: str, iid: int) -> dict:
        return await self._json("GET", f"/projects/{self._pid(project)}/merge_requests/{iid}")

    async def get_merge_request_diffs(self, project: str, iid: int) -> list[dict]:
        return await self._json("GET", f"/projects/{self._pid(project)}/merge_requests/{iid}/diffs", params={"per_page": 100})

    async def list_merge_requests(self, project: str, state: str = "opened", assignee: str | None = None, limit: int = 20) -> list[dict]:
        params: dict[str, Any] = {"state": state, "per_page": limit, "order_by": "updated_at"}
        if assignee:
            params["assignee_username"] = assignee
        return await self._json("GET", f"/projects/{self._pid(project)}/merge_requests", params=params)

    async def list_tree(self, project: str, ref: str, path: str = "") -> list[str]:
        items = await self._json("GET", f"/projects/{self._pid(project)}/repository/tree",
                                 params={"ref": ref, "path": path, "recursive": "true", "per_page": 100})
        return [item["path"] for item in items if item["type"] == "blob"]

    async def get_file(self, project: str, path: str, ref: str) -> str:
        response = await self._request("GET", f"/projects/{self._pid(project)}/repository/files/{quote(path, safe='')}/raw", params={"ref": ref})
        return response.text

    async def file_exists(self, project: str, path: str, ref: str) -> bool:
        try:
            await self.get_file(project, path, ref)
        except GitLabError as exc:
            if " 404" in str(exc):
                return False
            raise
        return True

    async def get_pipeline(self, project: str, pipeline_id: int) -> dict:
        return await self._json("GET", f"/projects/{self._pid(project)}/pipelines/{pipeline_id}")

    async def list_pipelines(self, project: str, ref: str | None = None, limit: int = 5) -> list[dict]:
        params: dict[str, Any] = {"per_page": limit}
        if ref:
            params["ref"] = ref
        return await self._json("GET", f"/projects/{self._pid(project)}/pipelines", params=params)

    async def list_pipeline_jobs(self, project: str, pipeline_id: int) -> list[dict]:
        return await self._json("GET", f"/projects/{self._pid(project)}/pipelines/{pipeline_id}/jobs", params={"per_page": 100})

    async def get_job_log(self, project: str, job_id: int) -> str:
        return (await self._request("GET", f"/projects/{self._pid(project)}/jobs/{job_id}/trace")).text

    async def list_deployments(self, project: str, limit: int = 10) -> list[dict]:
        return await self._json("GET", f"/projects/{self._pid(project)}/deployments",
                                params={"order_by": "updated_at", "sort": "desc", "per_page": limit})

    # ---- write (only what the allow-list permits)
    async def create_branch(self, project: str, branch: str, ref: str) -> dict:
        return await self._json("POST", f"/projects/{self._pid(project)}/repository/branches", params={"branch": branch, "ref": ref})

    async def commit_files(self, project: str, branch: str, message: str, files: dict[str, str], actions: dict[str, str]) -> dict:
        """Commit files. Only ever to an ai/* branch, so a person always reviews the change."""
        if not branch.startswith(AI_BRANCH_PREFIX):
            self.audit.event("tool_blocked", tool="gitlab", method="commit", branch=branch)
            raise GitLabError(f"Blocked: agents only commit to {AI_BRANCH_PREFIX}* branches, not {branch!r}.")
        payload = {
            "branch": branch,
            "commit_message": message,
            "actions": [{"action": actions[path], "file_path": path, "content": content} for path, content in files.items()],
        }
        return await self._json("POST", f"/projects/{self._pid(project)}/repository/commits", json=payload)

    async def trigger_pipeline(self, project: str, ref: str) -> dict:
        return await self._json("POST", f"/projects/{self._pid(project)}/pipeline", json={"ref": ref})

    async def wait_for_pipeline(self, project: str, pipeline_id: int) -> dict:
        deadline = asyncio.get_running_loop().time() + self.settings.pipeline_timeout_seconds
        while True:
            pipeline = await self.get_pipeline(project, pipeline_id)
            if pipeline["status"] in _FINISHED:
                return pipeline
            if asyncio.get_running_loop().time() > deadline:
                raise GitLabError(f"Pipeline {pipeline_id} did not finish within {self.settings.pipeline_timeout_seconds:.0f}s.")
            await asyncio.sleep(self.settings.pipeline_poll_seconds)

    async def create_merge_request(self, project: str, source: str, target: str, title: str, description: str) -> dict:
        return await self._json("POST", f"/projects/{self._pid(project)}/merge_requests",
                                json={"source_branch": source, "target_branch": target, "title": title, "description": description})

    async def comment_on_merge_request(self, project: str, iid: int, body: str) -> dict:
        return await self._json("POST", f"/projects/{self._pid(project)}/merge_requests/{iid}/notes", json={"body": body})

    async def create_issue(self, project: str, title: str, description: str) -> dict:
        return await self._json("POST", f"/projects/{self._pid(project)}/issues", json={"title": title, "description": description})
