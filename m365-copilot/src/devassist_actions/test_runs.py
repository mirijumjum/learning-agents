"""The test loop as a job, because a Copilot action can't wait for CI.

In the Foundry design one service writes the tests and runs the whole loop. Here
Copilot's model writes the test file and this module does everything that is a
fact rather than a judgement: commit to an ai/tests-* branch, trigger CI, wait,
count results, measure coverage and open the MR only when the pipeline is green.

  startTestRun ──► running ──► passed (MR opened by code, never by the model)
                      │
                      └──► failed ──► retryTestRun (new content) ──► running …
                              │                (at most N attempts, then gave_up)
                              └──► reportBugInCode ──► bug_reported (no MR)

Runs are kept in a JSON file so a restart keeps their status, and runs that were
still waiting for CI are picked up again on start.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from devassist.config import Settings
from devassist.gitlab import GitLabClient, GitLabError
from devassist.masking import mask
from devassist.specialists import SOURCE_EXTENSIONS, TestAgent, _TEST_CASE, _tail, _unique_branch, is_safe_test_path

logger = logging.getLogger("devassist_actions.test_runs")
MAX_TEST_FILE_CHARS = 100_000
FINAL = {"passed", "gave_up", "bug_reported", "error"}


class RunRefusal(ValueError):
    """A request we decline with a reason the user should read (HTTP 400/409)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TestRun:
    __test__ = False  # not a pytest test class

    id: str
    owner: str
    owner_name: str
    project: str
    source_ref: str
    label: str
    branch: str
    path: str
    max_attempts: int
    merge_request_iid: int | None = None
    attempt: int = 0
    status: str = "starting"
    pipeline_id: int | None = None
    pipeline_url: str = ""
    tests_added: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    coverage_before: float | None = None
    coverage_after: float | None = None
    failure_log: str = ""
    merge_request_url: str = ""
    message: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def public(self) -> dict[str, Any]:
        """What Copilot sees. Flat, and it says what to do next."""
        data = asdict(self)
        data.pop("owner")
        data["run_id"] = data.pop("id")
        data["attempts_left"] = max(0, self.max_attempts - self.attempt)
        data["next_step"] = {
            "starting": "Wait a few seconds and call getTestRun again.",
            "running": "CI is running. Tell the user, and call getTestRun again when they ask for the result.",
            "failed": "Read failure_log. If the test is wrong, call retryTestRun with the corrected file. If the code under test is wrong, call reportBugInCode.",
            "passed": "Report tests_added, tests_passed, tests_failed, coverage and the merge request link.",
            "gave_up": "Tell the user the tests still fail after every attempt; they are on the branch. No MR was opened.",
            "bug_reported": "Tell the user the code looks wrong; the failing tests are on the branch. No MR was opened.",
            "error": "Tell the user what went wrong (message).",
        }[self.status]
        return data


class RunStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._runs: dict[str, TestRun] = {}
        if path.exists():
            for item in json.loads(path.read_text(encoding="utf-8") or "[]"):
                run = TestRun(**item)
                self._runs[run.id] = run

    def get(self, run_id: str) -> TestRun | None:
        return self._runs.get(run_id)

    def all(self) -> list[TestRun]:
        return list(self._runs.values())

    def save(self, run: TestRun) -> None:
        run.updated_at = _now()
        with self._lock:
            self._runs[run.id] = run
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps([asdict(r) for r in self._runs.values()], indent=1), encoding="utf-8")
            temporary.replace(self.path)


class TestRunner:
    __test__ = False

    def __init__(self, gitlab: GitLabClient, settings: Settings, store: RunStore) -> None:
        self.gitlab, self.settings, self.store = gitlab, settings, store
        # Reuse the measuring half of the Foundry test agent: counts, coverage, failed-job log. No model is involved.
        self._measure = TestAgent(llm=None, gitlab=gitlab, settings=settings)  # type: ignore[arg-type]
        self._tasks: set[asyncio.Task] = set()

    # ---- validation shared by start and retry
    @staticmethod
    def _check_content(content: str) -> None:
        if not content.strip():
            raise RunRefusal("The test file is empty.")
        if len(content) > MAX_TEST_FILE_CHARS:
            raise RunRefusal(f"The test file has {len(content):,} characters, over the {MAX_TEST_FILE_CHARS:,} limit.")

    @staticmethod
    def _check_path(path: str) -> None:
        if not is_safe_test_path(path) or not path.endswith(SOURCE_EXTENSIONS):
            raise RunRefusal(f"{path!r} is not a test file. Only new files under a test folder or named test_* are allowed; never CI config or production code.")

    def _owned(self, run_id: str, owner: str) -> TestRun:
        run = self.store.get(run_id)
        if run is None or run.owner != owner:
            raise RunRefusal(f"There is no test run {run_id!r} for you.", status=404)
        return run

    # ---- operations
    async def start(self, owner: str, owner_name: str, project: str, path: str, content: str,
                    mr_iid: int | None = None, ref: str | None = None, label: str = "") -> TestRun:
        self._check_path(path)
        self._check_content(content)
        if mr_iid is not None:
            merge_request = await self.gitlab.get_merge_request(project, mr_iid)
            source_ref, label = merge_request["source_branch"], label or f"MR !{mr_iid}"
        else:
            source_ref = ref or (await self.gitlab.get_project(project))["default_branch"]
            label = label or source_ref
        if await self.gitlab.file_exists(project, path, source_ref):  # never overwrite a test a person wrote
            pure = PurePosixPath(path)
            path = str(pure.with_name(f"{pure.stem}_ai{pure.suffix}"))
        run = TestRun(
            id=uuid.uuid4().hex[:12], owner=owner, owner_name=owner_name, project=project, source_ref=source_ref,
            label=label, branch=_unique_branch("tests", label), path=path, max_attempts=self.settings.max_test_attempts,
            merge_request_iid=mr_iid, tests_added=len(_TEST_CASE.findall(content)),
        )
        self.store.save(run)
        self._spawn(self._first_attempt(run, content))
        return run

    async def retry(self, run_id: str, owner: str, content: str) -> TestRun:
        run = self._owned(run_id, owner)
        self._check_content(content)
        if run.status != "failed":
            raise RunRefusal(f"Run {run_id} is {run.status}; only a failed run can be retried.", status=409)
        if run.attempt >= run.max_attempts:
            raise RunRefusal(f"Run {run_id} already used its {run.max_attempts} attempts.", status=409)
        run.status, run.failure_log, run.tests_added = "running", "", len(_TEST_CASE.findall(content))
        self.store.save(run)
        self._spawn(self._attempt(run, content, "update"))
        return run

    async def report_bug(self, run_id: str, owner: str, summary: str) -> TestRun:
        run = self._owned(run_id, owner)
        if run.status != "failed":
            raise RunRefusal(f"Run {run_id} is {run.status}; a bug can only be reported after a failed pipeline.", status=409)
        run.status, run.message = "bug_reported", summary.strip()[:2000]
        self.store.save(run)
        return run

    def get(self, run_id: str, owner: str) -> TestRun:
        return self._owned(run_id, owner)

    def resume(self) -> int:
        """After a restart, keep waiting for the pipelines of runs that were in flight."""
        resumed = 0
        for run in self.store.all():
            if run.status == "running" and run.pipeline_id is not None:
                self._spawn(self._wait(run))
                resumed += 1
            elif run.status == "starting":
                run.status, run.message = "error", "The service restarted before the run started. Start a new run."
                self.store.save(run)
        return resumed

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---- background work
    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guard(self, run: TestRun, work: Any) -> None:
        try:
            await work
        except GitLabError as exc:
            run.status, run.message = "error", str(exc)[:500]
            self.store.save(run)
        except Exception:
            logger.exception("Test run %s failed", run.id)
            run.status, run.message = "error", "Unexpected error in the DevAssist actions API."
            self.store.save(run)

    async def _first_attempt(self, run: TestRun, content: str) -> None:
        async def work() -> None:
            run.coverage_before = await self._measure._baseline_coverage(run.project, run.source_ref)
            await self.gitlab.create_branch(run.project, run.branch, run.source_ref)
            await self._commit_and_wait(run, content, "create")
        await self._guard(run, work())

    async def _attempt(self, run: TestRun, content: str, action: str) -> None:
        await self._guard(run, self._commit_and_wait(run, content, action))

    async def _commit_and_wait(self, run: TestRun, content: str, action: str) -> None:
        run.attempt += 1
        await self.gitlab.commit_files(run.project, run.branch, f"test: AI-generated tests for {run.label} (attempt {run.attempt})",
                                       {run.path: content}, {run.path: action})
        started = await self.gitlab.trigger_pipeline(run.project, run.branch)
        run.status, run.pipeline_id, run.pipeline_url = "running", started["id"], started.get("web_url", "")
        self.store.save(run)
        await self._wait(run)

    async def _wait(self, run: TestRun) -> None:
        async def work() -> None:
            pipeline = await self.gitlab.wait_for_pipeline(run.project, run.pipeline_id)  # type: ignore[arg-type]
            run.pipeline_url = pipeline.get("web_url", run.pipeline_url)
            run.tests_passed, run.tests_failed = await self._measure._test_counts(run.project, pipeline["id"])
            if pipeline["status"] == "success":
                after = pipeline.get("coverage")
                run.coverage_after = float(after) if after is not None else None
                merge_request = await self.gitlab.create_merge_request(
                    run.project, run.branch, run.source_ref, f"AI tests for {run.label}"[:200],
                    f"Generated by DevAssist in Microsoft 365 Copilot for {run.owner_name}. Pipeline: {run.pipeline_url}\n\nPlease review before merging.",
                )
                run.status, run.merge_request_url = "passed", merge_request["web_url"]
            else:
                run.failure_log = mask(_tail(await self._measure._failed_job_log(run.project, pipeline["id"]), 4_000))
                run.status = "gave_up" if run.attempt >= run.max_attempts else "failed"
            self.store.save(run)
        await self._guard(run, work())
