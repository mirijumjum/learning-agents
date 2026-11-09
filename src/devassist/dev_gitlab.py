"""A local stand-in for GitLab, so the whole system runs with no account.

It speaks the subset of the real REST API that DevAssist uses, and its "CI"
really runs pytest on the branch contents. It executes generated code on this
machine and has no authentication, so it only listens on 127.0.0.1. In
production the code runs on your GitLab runners instead.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

_ORDER_SERVICE = '''class OrderService:
    def __init__(self, tax_rate: float = 0.21) -> None:
        self.tax_rate = tax_rate

    def subtotal(self, items: list[dict]) -> float:
        return sum(item["price"] * item["quantity"] for item in items)

    def apply_discount(self, amount: float, percent: float) -> float:
        if not 0 <= percent <= 100:
            raise ValueError("percent must be between 0 and 100")
        return amount * (1 - percent / 100)

    def total(self, items: list[dict], discount_percent: float = 0.0) -> float:
        discounted = self.apply_discount(self.subtotal(items), discount_percent)
        return round(discounted * (1 + self.tax_rate), 2)
'''
_ORDER_SERVICE_BASE = _ORDER_SERVICE.split("    def apply_discount")[0] + '''    def total(self, items: list[dict]) -> float:
        return round(self.subtotal(items) * (1 + self.tax_rate), 2)
'''
_CUSTOMERS = '''"""Customer endpoints."""
from portal.web import route

_CUSTOMERS = {}


@route("GET", "/customers")
def list_customers():
    return list(_CUSTOMERS.values())


@route("GET", "/customers/<id>")
def get_customer(id):
    if id not in _CUSTOMERS:
        return {"error": "not found"}, 404
    return _CUSTOMERS[id]


@route("POST", "/customers")
def create_customer(body):
    _CUSTOMERS[body["id"]] = body
    return body, 201
'''
_FAILED_JOB_LOG = """$ pip install -r requirements.txt
Successfully installed pytest-8.3.2
$ python -m pytest -q
ImportError while loading conftest '/builds/acme/payments-api/tests/conftest.py'.
tests/conftest.py:3: in <module>
    from app.settings import DATABASE_URL
E   ModuleNotFoundError: No module named 'app.settings'
ERROR: Job failed: exit code 4
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class FakeGitLab:
    def __init__(self, base_url: str = "http://127.0.0.1:8929") -> None:
        self.base_url = base_url
        self.projects: dict[str, dict[str, Any]] = {}
        self.pipelines: dict[int, dict[str, Any]] = {}
        self.jobs: dict[int, dict[str, Any]] = {}
        self._next_pipeline = 6000
        self._tasks: set[asyncio.Task] = set()
        self._seed()

    def _project(self, path: str, default: str, branches: dict[str, dict[str, str]]) -> dict[str, Any]:
        project = {"id": len(self.projects) + 1, "path": path.rsplit("/", 1)[-1], "path_with_namespace": path, "default_branch": default,
                   "branches": branches, "mrs": {}, "issues": [], "deployments": [], "notes": []}
        self.projects[path] = project
        return project

    def _url(self, project: dict[str, Any], suffix: str) -> str:
        return f"{self.base_url}/{project['path_with_namespace']}/-/{suffix}"

    def _add_mr(self, project: dict[str, Any], iid: int, title: str, source: str, target: str, state: str = "opened", assignee: str | None = None) -> dict[str, Any]:
        mr = {"iid": iid, "title": title, "source_branch": source, "target_branch": target, "state": state, "assignee": assignee,
              "web_url": self._url(project, f"merge_requests/{iid}"), "description": ""}
        project["mrs"][iid] = mr
        return mr

    def _seed(self) -> None:
        base = {
            "README.md": "# payments-api\n",
            "app/__init__.py": "",
            "app/order_service.py": _ORDER_SERVICE_BASE,
            "tests/__init__.py": "",
            "tests/test_smoke.py": 'from app.order_service import OrderService\n\n\ndef test_default_tax():\n    assert OrderService().tax_rate == 0.21\n',
        }
        payments = self._project("acme/payments-api", "develop", {
            "develop": dict(base),
            "feature/order-discount": {**base, "app/order_service.py": _ORDER_SERVICE},
            "feature/buggy-discount": {**base, "app/order_service.py": _ORDER_SERVICE.replace("percent / 100", "percent / 10")},
        })
        self._add_mr(payments, 231, "Add order discounts", "feature/order-discount", "develop", assignee="dev")
        self._add_mr(payments, 232, "Discount rework", "feature/buggy-discount", "develop", assignee="sam")
        self._add_mr(payments, 220, "Fix rounding of totals", "feature/rounding", "develop", state="merged")
        self._add_mr(payments, 218, "Add tax rate setting", "feature/tax", "develop", state="merged")
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        payments["deployments"].append({"id": 1, "created_at": yesterday.replace(hour=16, minute=52, second=0, microsecond=0).isoformat(), "ref": "develop",
                                        "sha": "9f3c2a71b0e4", "environment": {"name": "production"}, "status": "success"})

        self._project("acme/customer-portal", "main", {"main": {
            "README.md": "# customer-portal\n", "portal/__init__.py": "", "controllers/__init__.py": "", "controllers/customers.py": _CUSTOMERS,
        }})

        self.pipelines[5512] = {"id": 5512, "status": "failed", "ref": "develop", "coverage": None, "project": "acme/payments-api",
                                "web_url": self._url(payments, "pipelines/5512")}
        self.jobs[77001] = {"id": 77001, "name": "unit-tests", "stage": "test", "status": "failed", "pipeline_id": 5512, "log": _FAILED_JOB_LOG,
                            "web_url": self._url(payments, "jobs/77001")}
        # What the feature branch's last ordinary CI run reported, for "coverage before".
        self.pipelines[5400] = {"id": 5400, "status": "success", "ref": "feature/order-discount", "coverage": "38", "project": "acme/payments-api",
                                "web_url": self._url(payments, "pipelines/5400")}

    @staticmethod
    def _error(status: int, message: str) -> web.Response:
        return web.json_response({"message": message}, status=status)

    def _get(self, request: web.Request) -> dict[str, Any] | None:
        return self.projects.get(request.match_info["project"])

    # --- CI: really runs pytest
    async def _run_ci(self, project: dict[str, Any], ref: str, pipeline: dict[str, Any], job: dict[str, Any]) -> None:
        with tempfile.TemporaryDirectory(prefix="fake-gitlab-ci-") as tmp:
            root = Path(tmp)
            for path, content in project["branches"][ref].items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            (root / ".coveragerc").write_text("[run]\nomit = tests/*\n", encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "--cov=.", "--cov-report=term", cwd=root,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            try:
                raw, _ = await asyncio.wait_for(process.communicate(), timeout=120)
                code, output = process.returncode, raw.decode(errors="replace")
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                code, output = 1, "Job timed out after 120s"
        match = re.search(r"^TOTAL\s+.*?(\d+)%", output, re.M)
        coverage = match.group(1) if match else None
        status = "success" if code in (0, 5) else "failed"  # 5 = pytest found no tests
        job.update(status=status, log=f"$ python -m pytest\n{output}")
        pipeline.update(status=status, coverage=coverage)

    def _start_pipeline(self, project: dict[str, Any], ref: str) -> dict[str, Any]:
        self._next_pipeline += 1
        pipeline_id = self._next_pipeline
        pipeline = {"id": pipeline_id, "status": "running", "ref": ref, "coverage": None, "project": project["path_with_namespace"],
                    "web_url": self._url(project, f"pipelines/{pipeline_id}")}
        job = {"id": pipeline_id * 10, "name": "unit-tests", "stage": "test", "status": "running", "pipeline_id": pipeline_id, "log": "",
               "web_url": self._url(project, f"jobs/{pipeline_id * 10}")}
        self.pipelines[pipeline_id] = pipeline
        self.jobs[job["id"]] = job
        task = asyncio.create_task(self._run_ci(project, ref, pipeline, job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return pipeline

    # --- routes
    async def list_projects(self, request: web.Request) -> web.Response:
        search = request.query.get("search", "")
        return web.json_response([{"path": p["path"], "path_with_namespace": p["path_with_namespace"]} for p in self.projects.values() if search in p["path"]])

    async def project(self, request: web.Request) -> web.Response:
        project = self._get(request)
        if project is None:
            return self._error(404, "404 Project Not Found")
        return web.json_response({"id": project["id"], "path": project["path"], "path_with_namespace": project["path_with_namespace"], "default_branch": project["default_branch"]})

    async def tree(self, request: web.Request) -> web.Response:
        project = self._get(request)
        ref = request.query.get("ref", "")
        if project is None or ref not in project["branches"]:
            return self._error(404, "404 Tree Not Found")
        return web.json_response([{"path": path, "type": "blob"} for path in sorted(project["branches"][ref])])

    async def raw_file(self, request: web.Request) -> web.Response:
        project = self._get(request)
        ref = request.query.get("ref", "")
        path = request.match_info["file"]
        if project is None or path not in project["branches"].get(ref, {}):
            return self._error(404, "404 File Not Found")
        return web.Response(text=project["branches"][ref][path])

    async def create_branch(self, request: web.Request) -> web.Response:
        project = self._get(request)
        name, ref = request.query.get("branch", ""), request.query.get("ref", "")
        if project is None or ref not in project["branches"]:
            return self._error(400, "Invalid reference name")
        if name in project["branches"]:
            return self._error(400, "Branch already exists")
        project["branches"][name] = dict(project["branches"][ref])
        return web.json_response({"name": name, "web_url": self._url(project, f"tree/{name}")}, status=201)

    async def commit(self, request: web.Request) -> web.Response:
        project = self._get(request)
        body = await request.json()
        if project is None or body["branch"] not in project["branches"]:
            return self._error(400, "Branch not found")
        files = project["branches"][body["branch"]]
        for action in body["actions"]:
            exists = action["file_path"] in files
            if (action["action"] == "create" and exists) or (action["action"] == "update" and not exists):
                return self._error(400, f"Invalid {action['action']} for {action['file_path']}")
            files[action["file_path"]] = action["content"]
        return web.json_response({"id": "abc123", "web_url": self._url(project, "commit/abc123")}, status=201)

    async def list_mrs(self, request: web.Request) -> web.Response:
        project = self._get(request)
        if project is None:
            return self._error(404, "404 Project Not Found")
        state, assignee = request.query.get("state", "all"), request.query.get("assignee_username")
        return web.json_response([{k: v for k, v in mr.items()} for mr in project["mrs"].values()
                                  if (state == "all" or mr["state"] == state) and (not assignee or mr["assignee"] == assignee)])

    async def create_mr(self, request: web.Request) -> web.Response:
        project = self._get(request)
        if project is None:
            return self._error(404, "404 Project Not Found")
        body = await request.json()
        iid = max(project["mrs"], default=0) + 1
        mr = self._add_mr(project, iid, body["title"], body["source_branch"], body["target_branch"])
        mr["description"] = body.get("description", "")
        return web.json_response(mr, status=201)

    async def get_mr(self, request: web.Request) -> web.Response:
        project = self._get(request)
        mr = project["mrs"].get(int(request.match_info["iid"])) if project else None
        return web.json_response(mr) if mr else self._error(404, "404 Merge Request Not Found")

    async def mr_diffs(self, request: web.Request) -> web.Response:
        project = self._get(request)
        mr = project["mrs"].get(int(request.match_info["iid"])) if project else None
        if mr is None or mr["source_branch"] not in project["branches"]:  # type: ignore[index]
            return self._error(404, "404 Merge Request Not Found")
        target, source = project["branches"][mr["target_branch"]], project["branches"][mr["source_branch"]]  # type: ignore[index]
        diffs = []
        for path in sorted(set(target) | set(source)):
            if target.get(path) != source.get(path):
                diff = "".join(difflib.unified_diff(target.get(path, "").splitlines(True), source.get(path, "").splitlines(True), n=2))
                diffs.append({"new_path": path, "old_path": path, "deleted_file": path not in source, "diff": diff})
        return web.json_response(diffs)

    async def add_note(self, request: web.Request) -> web.Response:
        project = self._get(request)
        if project is None:
            return self._error(404, "404 Project Not Found")
        body = await request.json()
        project["notes"].append({"mr": int(request.match_info["iid"]), "body": body["body"]})
        return web.json_response({"id": len(project["notes"])}, status=201)

    async def create_issue(self, request: web.Request) -> web.Response:
        project = self._get(request)
        if project is None:
            return self._error(404, "404 Project Not Found")
        body = await request.json()
        iid = len(project["issues"]) + 1
        issue = {"iid": iid, "title": body["title"], "description": body.get("description", ""), "web_url": self._url(project, f"issues/{iid}")}
        project["issues"].append(issue)
        return web.json_response(issue, status=201)

    async def trigger(self, request: web.Request) -> web.Response:
        project = self._get(request)
        body = await request.json()
        if project is None or body["ref"] not in project["branches"]:
            return self._error(400, "Reference not found")
        return web.json_response(self._start_pipeline(project, body["ref"]), status=201)

    async def list_pipelines(self, request: web.Request) -> web.Response:
        path, ref = request.match_info["project"], request.query.get("ref")
        items = [p for p in self.pipelines.values() if p["project"] == path and (not ref or p["ref"] == ref)]
        return web.json_response(sorted(items, key=lambda p: p["id"], reverse=True))

    async def get_pipeline(self, request: web.Request) -> web.Response:
        pipeline = self.pipelines.get(int(request.match_info["id"]))
        return web.json_response(pipeline) if pipeline else self._error(404, "404 Not found")

    async def pipeline_jobs(self, request: web.Request) -> web.Response:
        pipeline_id = int(request.match_info["id"])
        return web.json_response([{k: v for k, v in job.items() if k != "log"} for job in self.jobs.values() if job["pipeline_id"] == pipeline_id])

    async def job_trace(self, request: web.Request) -> web.Response:
        job = self.jobs.get(int(request.match_info["id"]))
        return web.Response(text=job["log"]) if job else self._error(404, "404 Not found")

    async def deployments(self, request: web.Request) -> web.Response:
        project = self._get(request)
        return web.json_response(project["deployments"] if project else [])

    def app(self) -> web.Application:
        app = web.Application()
        prefix = "/api/v4/projects"
        app.add_routes([
            web.get(prefix, self.list_projects),
            web.get(prefix + "/{project}", self.project),
            web.get(prefix + "/{project}/repository/tree", self.tree),
            web.get(prefix + "/{project}/repository/files/{file}/raw", self.raw_file),
            web.post(prefix + "/{project}/repository/branches", self.create_branch),
            web.post(prefix + "/{project}/repository/commits", self.commit),
            web.get(prefix + "/{project}/merge_requests", self.list_mrs),
            web.post(prefix + "/{project}/merge_requests", self.create_mr),
            web.get(prefix + "/{project}/merge_requests/{iid:\\d+}", self.get_mr),
            web.get(prefix + "/{project}/merge_requests/{iid:\\d+}/diffs", self.mr_diffs),
            web.post(prefix + "/{project}/merge_requests/{iid:\\d+}/notes", self.add_note),
            web.post(prefix + "/{project}/issues", self.create_issue),
            web.post(prefix + "/{project}/pipeline", self.trigger),
            web.get(prefix + "/{project}/pipelines", self.list_pipelines),
            web.get(prefix + "/{project}/pipelines/{id:\\d+}", self.get_pipeline),
            web.get(prefix + "/{project}/pipelines/{id:\\d+}/jobs", self.pipeline_jobs),
            web.get(prefix + "/{project}/jobs/{id:\\d+}/trace", self.job_trace),
            web.get(prefix + "/{project}/deployments", self.deployments),
        ])
        return app


def seed_sample_logs(path: Path, now: datetime | None = None) -> int:
    """Write logs with an incident yesterday from 17:05 to 17:40 UTC. Contains fake personal data, to prove masking."""
    now = now or datetime.now(timezone.utc)
    day = (now - timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []

    def emit(minutes: float, level: str, service: str, message: str) -> None:
        lines.append(json.dumps({"timestamp": (day + timedelta(minutes=minutes)).isoformat(), "level": level, "service": service, "message": message, "trace_id": f"t{len(lines):05d}"}))

    for minute in range(0, 60, 3):
        emit(minute, "INFO", "checkout", f"checkout completed in {120 + minute} ms")
        emit(minute, "INFO", "catalog", "price list refreshed")
    for index in range(42):
        emit(5 + index * 0.8, "ERROR", "checkout", f"DB connection pool exhausted (timeout 3000 ms) for customer jane.doe{index}@example.com from 10.0.4.{index}")
    for index in range(7):
        emit(9 + index * 4, "ERROR", "checkout", f"PaymentGatewayTimeout: no response after {5000 + index} ms customer_id=C{9000 + index}")
    emit(20, "WARNING", "catalog", "slow query 900 ms")
    with path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return len(lines)


async def serve_fake_gitlab(host: str, port: int) -> None:
    fake = FakeGitLab(base_url=f"http://{host}:{port}")
    runner = web.AppRunner(fake.app())
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    await asyncio.Event().wait()
