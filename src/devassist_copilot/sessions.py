"""State of each test-generation loop.

In the Foundry design one process runs the whole loop. Here the Copilot Studio
model writes the tests across several tool calls, so the backend keeps the state
and enforces the rules between calls: one ai/* branch per session, one test file,
a fixed number of attempts, and no merge request unless CI passed.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ready: branch created, nothing committed yet. running: a pipeline is in flight.
# failed: last pipeline red, attempts left. exhausted: red after the last attempt.
# passed: last pipeline green. mr_opened / bug_reported: finished.
STATUSES = ("ready", "running", "failed", "exhausted", "passed", "mr_opened", "bug_reported")
FINISHED = ("exhausted", "mr_opened", "bug_reported")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TestSession:
    __test__ = False  # not a pytest test class

    id: str
    project: str
    source_ref: str
    branch: str
    label: str
    created_by: str
    max_attempts: int
    coverage_before: float | None = None
    status: str = "ready"
    attempt: int = 0
    test_path: str = ""
    content: str = ""
    pipeline_id: int | None = None
    pipeline_url: str = ""
    coverage_after: float | None = None
    passing: int = 0
    failing: int = 0
    failed_log: str = ""
    merge_request_url: str = ""
    summary: str = ""
    created_at: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))


class SessionStore:
    """Sessions in memory, mirrored to a JSON file so a restart does not lose a running loop.

    One replica only: two replicas would each have their own file.
    """

    def __init__(self, path: Path | None = None, ttl: timedelta = timedelta(hours=24)) -> None:
        self.path = path
        self.ttl = ttl
        self._lock = threading.Lock()
        self._sessions: dict[str, TestSession] = {}
        if path and path.exists():
            for raw in json.loads(path.read_text(encoding="utf-8")):
                session = TestSession(**raw)
                self._sessions[session.id] = session

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(s) for s in self._sessions.values()], ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def _expire(self) -> None:
        cutoff = _now() - self.ttl
        for key in [k for k, s in self._sessions.items() if datetime.fromisoformat(s.created_at) < cutoff]:
            del self._sessions[key]

    def create(self, **fields: object) -> TestSession:
        with self._lock:
            self._expire()
            session = TestSession(id=uuid.uuid4().hex[:12], **fields)  # type: ignore[arg-type]
            self._sessions[session.id] = session
            self._save()
            return session

    def get(self, session_id: str) -> TestSession | None:
        with self._lock:
            return self._sessions.get(session_id.strip())

    def save(self, session: TestSession) -> None:
        with self._lock:
            self._sessions[session.id] = session
            self._save()
