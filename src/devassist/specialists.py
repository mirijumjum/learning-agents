"""The four specialist agents.

Facts are measured by code and only judgement goes to the model: test results
come from the pipeline, error counts from the log store, links from GitLab.
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import PurePosixPath
from typing import Annotated, Any

from agent_framework import tool
from pydantic import Field

from devassist.audit import RequestContext
from devassist.config import Settings
from devassist.gitlab import GitLabClient, GitLabError
from devassist.llm import LLM
from devassist.logstore import LogEntry, LogStore, group_errors, parse_time_range
from devassist.masking import mask

SOURCE_EXTENSIONS = (".py", ".js", ".ts", ".java", ".cs", ".go", ".rb", ".php", ".kt")
_FILE_BLOCK = re.compile(r"FILE:[*\s`]*([^\s`*]+)[`*\s]*\n```[\w+-]*\n(.*?)```", re.S)
_CODE_WRONG = re.compile(r"CODE_WRONG:\s*(.+)", re.S)
_URL = re.compile(r"\[([^\]]*)\]\(https?://[^)]*\)|https?://[^\s)>\]]+")
_FENCE = re.compile(r"^```[\w-]*\n(.*?)\n?```\s*$", re.S)
_API_FILE = re.compile(r"controller|route|endpoint|views?\b|api", re.I)
_TEST_CASE = re.compile(r"^\s*(?:async\s+)?def test_|@Test\b|@ParameterizedTest\b|\[(?:Fact|Theory|Test)\]|^\s*(?:it|test)\(|^func Test", re.M)


class AgentRefusal(RuntimeError):
    """A request we decline for a reason the user should read, e.g. a diff over the size limit."""


def instructions(name: str) -> str:
    return resources.files("devassist.instructions").joinpath(f"{name}.md").read_text(encoding="utf-8")


@dataclass
class Reply:
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)

    def render(self) -> str:
        lines = [self.text.strip()]
        if self.links:
            lines.append("")
            lines.extend(f"- [{label}]({url})" for label, url in self.links)
        return "\n".join(lines)


def drop_unverified_urls(text: str, known: set[str]) -> str:
    """Local models invent plausible links. Only URLs that came from GitLab may reach the user."""

    def replace(match: re.Match[str]) -> str:
        url = re.search(r"https?://[^\s)>\]]+", match.group(0)).group(0)  # type: ignore[union-attr]
        if url in known:
            return match.group(0)
        return match.group(1) or ""

    return _URL.sub(replace, text)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "change"


def _unique_branch(kind: str, label: str) -> str:
    return f"ai/{kind}-{_slug(label)}-{uuid.uuid4().hex[:6]}"


def _tail(text: str, chars: int = 6_000) -> str:
    return text if len(text) <= chars else "...(earlier output omitted)\n" + text[-chars:]


def is_safe_test_path(path: str) -> bool:
    """The test agent may only add test files, never CI config or production code."""
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or pure.name.startswith(".") or "\\" in path:
        return False
    return "tests" in pure.parts or "test" in pure.parts or "test" in pure.name.lower()


def _code_without_docs(source: str) -> str:
    """AST of a module with docstrings removed, to prove only documentation changed."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                node.body = node.body[1:] or [ast.Pass()]
    return ast.dump(tree)


class GitAgent:
    def __init__(self, llm: LLM, gitlab: GitLabClient, settings: Settings) -> None:
        self.llm, self.gitlab, self.settings = llm, gitlab, settings

    def _username(self, context: RequestContext) -> str:
        if context.email and context.email in self.settings.user_map:
            return self.settings.user_map[context.email]
        return context.email.split("@")[0] if context.email else context.user

    async def run(self, text: str, context: RequestContext) -> Reply:
        links: list[tuple[str, str]] = []
        gitlab = self.gitlab
        username = self._username(context)
        default = self.settings.default_project

        async def guarded(action: Any) -> str:
            try:
                return json.dumps(await action(), ensure_ascii=False)
            except GitLabError as exc:
                return f"ERROR: {exc}"

        async def list_merge_requests(
            project: Annotated[str, Field(description="Project name, e.g. payments-api")] = default,
            assigned_to_me: Annotated[bool, Field(description="Only merge requests assigned to the person asking")] = False,
            state: Annotated[str, Field(description="opened, merged, closed or all")] = "opened",
        ) -> str:
            """List merge requests of a project."""
            async def action() -> list[dict]:
                path = await gitlab.resolve_project(project)
                items = await gitlab.list_merge_requests(path, state=state, assignee=username if assigned_to_me else None)
                links.extend((f"!{mr['iid']} {mr['title']}", mr["web_url"]) for mr in items)
                return [{"iid": mr["iid"], "title": mr["title"], "state": mr["state"], "source_branch": mr["source_branch"]} for mr in items]
            return await guarded(action)

        async def create_branch(
            branch: Annotated[str, Field(description="Name of the new branch")],
            ref: Annotated[str, Field(description="Branch to create it from")],
            project: Annotated[str, Field(description="Project name")] = default,
        ) -> str:
            """Create a branch from an existing branch."""
            async def action() -> dict:
                path = await gitlab.resolve_project(project)
                created = await gitlab.create_branch(path, branch, ref)
                links.append((f"Branch {branch}", created["web_url"]))
                return {"created": branch, "from": ref}
            return await guarded(action)

        async def pipeline_status(
            pipeline_id: Annotated[int, Field(description="Pipeline number")],
            project: Annotated[str, Field(description="Project name")] = default,
        ) -> str:
            """Get the status of one pipeline."""
            async def action() -> dict:
                path = await gitlab.resolve_project(project)
                pipeline = await gitlab.get_pipeline(path, pipeline_id)
                links.append((f"Pipeline #{pipeline_id}", pipeline["web_url"]))
                return {"id": pipeline_id, "status": pipeline["status"], "ref": pipeline["ref"]}
            return await guarded(action)

        async def comment_on_merge_request(
            iid: Annotated[int, Field(description="Merge request number")],
            body: Annotated[str, Field(description="Comment text")],
            project: Annotated[str, Field(description="Project name")] = default,
        ) -> str:
            """Add a comment to a merge request."""
            async def action() -> dict:
                path = await gitlab.resolve_project(project)
                await gitlab.comment_on_merge_request(path, iid, body)
                merge_request = await gitlab.get_merge_request(path, iid)
                links.append((f"!{iid}", merge_request["web_url"]))
                return {"commented_on": iid}
            return await guarded(action)

        async def create_issue(
            title: Annotated[str, Field(description="Issue title")],
            description: Annotated[str, Field(description="Issue description")] = "",
            project: Annotated[str, Field(description="Project name")] = default,
        ) -> str:
            """Create an issue in a project."""
            async def action() -> dict:
                path = await gitlab.resolve_project(project)
                issue = await gitlab.create_issue(path, title, description)
                links.append((f"Issue #{issue['iid']}", issue["web_url"]))
                return {"created_issue": issue["iid"], "title": title}
            return await guarded(action)

        tools = [tool(fn, name=fn.__name__) for fn in (list_merge_requests, create_branch, pipeline_status, comment_on_merge_request, create_issue)]
        answer = await self.llm.ask("git", instructions("git"), text, tools=tools)
        return Reply(drop_unverified_urls(answer, {url for _, url in links}), links)


@dataclass
class TestOutcome:
    reply: Reply
    passed: bool
    bug_found: bool = False


class TestAgent:
    """The fixed loop from the design: write, commit, let CI run it, read, fix up to N times, report."""

    def __init__(self, llm: LLM, gitlab: GitLabClient, settings: Settings) -> None:
        self.llm, self.gitlab, self.settings = llm, gitlab, settings

    async def _gather_from_mr(self, project: str, iid: int) -> tuple[str, str, str, list[str]]:
        merge_request = await self.gitlab.get_merge_request(project, iid)
        diffs = await self.gitlab.get_merge_request_diffs(project, iid)
        diff_text = "\n".join(f"--- {d['new_path']}\n{d['diff']}" for d in diffs)
        if len(diff_text) > self.settings.max_diff_chars:
            raise AgentRefusal(
                f"MR !{iid} has a {len(diff_text):,} character diff, over the {self.settings.max_diff_chars:,} limit. "
                "Name a smaller part of it, for example one class or file."
            )
        files = [d["new_path"] for d in diffs if not d.get("deleted_file") and d["new_path"].endswith(SOURCE_EXTENSIONS) and not is_safe_test_path(d["new_path"])]
        return merge_request["source_branch"], f"MR !{iid}: {merge_request['title']}", diff_text, files

    async def _gather_from_symbol(self, project: str, ref: str, symbol: str) -> list[str]:
        paths = [p for p in await self.gitlab.list_tree(project, ref) if p.endswith(SOURCE_EXTENSIONS) and not is_safe_test_path(p)]
        hits = []
        for path in paths[:40]:
            if symbol and symbol in await self.gitlab.get_file(project, path, ref):
                hits.append(path)
        return hits or paths[:5]

    @staticmethod
    def _parse_file(answer: str) -> tuple[str, str]:
        match = _FILE_BLOCK.search(answer)
        if not match:
            raise AgentRefusal("The model did not return a test file in the expected format, so nothing was committed.")
        path, content = match.group(1).strip("`"), match.group(2)
        if not is_safe_test_path(path):
            raise AgentRefusal(f"The model proposed writing to {path!r}, which is not a test file, so nothing was committed.")
        return path, content

    async def _ask_for_file(self, prompt: str, deliberate: bool = False) -> str:
        """Ask the model, giving it one more chance if the reply can't be parsed."""
        answer = await self.llm.ask("test", instructions("test"), prompt, deliberate=deliberate)
        if _FILE_BLOCK.search(answer) or _CODE_WRONG.search(answer):
            return answer
        # Reformatting needs no deliberation, and a reasoning retry is where local models run away.
        return await self.llm.ask("test", instructions("test"), f"{prompt}\n\nYour previous reply was not in the required format. Reply only with the FILE line and one complete code block, or the single CODE_WRONG line.")

    async def _failed_job_log(self, project: str, pipeline_id: int) -> str:
        jobs = await self.gitlab.list_pipeline_jobs(project, pipeline_id)
        failed = [job for job in jobs if job["status"] == "failed"] or jobs
        if not failed:
            return "(the pipeline has no jobs)"
        return mask(_tail(await self.gitlab.get_job_log(project, failed[0]["id"])))

    async def _test_counts(self, project: str, pipeline_id: int) -> tuple[int, int]:
        log = ""
        for job in await self.gitlab.list_pipeline_jobs(project, pipeline_id):
            log += await self.gitlab.get_job_log(project, job["id"])
        passed = re.search(r"(\d+) passed", log)
        failed = re.search(r"(\d+) failed", log)
        if passed or failed:
            return int(passed.group(1)) if passed else 0, int(failed.group(1)) if failed else 0
        junit = re.search(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)", log)
        if junit:
            total, failures, errors = map(int, junit.groups())
            return total - failures - errors, failures + errors
        return 0, 0

    async def _baseline_coverage(self, project: str, ref: str) -> float | None:
        for pipeline in await self.gitlab.list_pipelines(project, ref=ref):
            if pipeline["status"] == "success":
                detail = await self.gitlab.get_pipeline(project, pipeline["id"])
                if detail.get("coverage") is not None:
                    return float(detail["coverage"])
        return None

    async def run(self, project: str, *, mr_iid: int | None = None, ref: str | None = None, symbol: str = "") -> TestOutcome:
        gitlab = self.gitlab
        diff_text = ""
        if mr_iid is not None:
            source_ref, label, diff_text, files = await self._gather_from_mr(project, mr_iid)
        else:
            source_ref = ref or (await gitlab.get_project(project))["default_branch"]
            files = await self._gather_from_symbol(project, source_ref, symbol)
            label = symbol or source_ref
        if not files:
            raise AgentRefusal("I found no source files to test in that change.")

        sources = "\n\n".join([f"### {p}\n{await gitlab.get_file(project, p, source_ref)}" for p in files[:8]])
        tree = await gitlab.list_tree(project, source_ref)
        examples = [p for p in tree if is_safe_test_path(p) and p.endswith(SOURCE_EXTENSIONS) and not p.endswith("__init__.py")]
        style = f"\n\nExisting test, for style ({examples[0]}):\n{await gitlab.get_file(project, examples[0], source_ref)}" if examples else ""
        rules = ""
        if ".devassist/testing-rules.md" in tree:
            rules = "\n\nTeam testing rules:\n" + await gitlab.get_file(project, ".devassist/testing-rules.md", source_ref)
        focus = f"\n\nFocus on: {symbol}" if symbol else ""
        base_prompt = f"Write unit tests for {label}.{focus}\n\nDiff:\n{diff_text or '(none)'}\n\nSource files:\n{sources}{style}{rules}"

        before = await self._baseline_coverage(project, source_ref)
        branch = _unique_branch("tests", label)
        await gitlab.create_branch(project, branch, source_ref)

        path, content = self._parse_file(await self._ask_for_file(base_prompt))
        if await gitlab.file_exists(project, path, source_ref):  # never overwrite a test a person wrote
            pure = PurePosixPath(path)
            path = str(pure.with_name(f"{pure.stem}_ai{pure.suffix}"))

        pipeline: dict[str, Any] = {}
        action = "create"
        for attempt in range(1, self.settings.max_test_attempts + 1):
            await gitlab.commit_files(project, branch, f"test: AI-generated tests for {label} (attempt {attempt})", {path: content}, {path: action})
            action = "update"
            started = await gitlab.trigger_pipeline(project, branch)
            pipeline = await gitlab.wait_for_pipeline(project, started["id"])
            if pipeline["status"] == "success":
                break

            job_log = await self._failed_job_log(project, pipeline["id"])
            if attempt == self.settings.max_test_attempts:
                return TestOutcome(Reply(
                    f"The tests still fail after {attempt} attempts, so I did not open an MR. They are on `{branch}` if you want to look.\n\n```\n{_tail(job_log, 800)}\n```",
                    [("Last pipeline", pipeline["web_url"])],
                ), passed=False)

            # Blaming innocent code misleads people, so this one decision gets real reasoning.
            answer = await self._ask_for_file(f"{base_prompt}\n\nCurrent test file ({path}):\n{content}\n\nFailed job log:\n{job_log}", deliberate=True)
            bug = _CODE_WRONG.search(answer)
            if bug:
                return TestOutcome(Reply(
                    f"I stopped because this looks like a bug in the code, not in the tests:\n\n{bug.group(1).strip()}\n\nThe failing tests are on `{branch}`.",
                    [("Pipeline showing the failure", pipeline["web_url"])],
                ), passed=False, bug_found=True)
            content = self._parse_file(answer)[1]

        passed, failed = await self._test_counts(project, pipeline["id"])
        after = pipeline.get("coverage")
        coverage = ""
        if after is not None:
            coverage = f" Coverage {before:.0f}% → {float(after):.0f}%." if before is not None else f" Coverage {float(after):.0f}%."
        title = f"AI tests for {label}"[:200]
        description = f"Generated by DevAssist. Pipeline: {pipeline['web_url']}\n\nPlease review before merging."
        merge_request = await gitlab.create_merge_request(project, branch, source_ref, title, description)
        added = len(_TEST_CASE.findall(content))
        return TestOutcome(Reply(
            f"{added} tests added. Pipeline: {passed} passing, {failed} failing.{coverage}",
            [("Merge request", merge_request["web_url"]), ("Pipeline", pipeline["web_url"])],
        ), passed=True)


class DocsAgent:
    def __init__(self, llm: LLM, gitlab: GitLabClient, settings: Settings) -> None:
        self.llm, self.gitlab, self.settings = llm, gitlab, settings

    async def _open_mr(self, project: str, kind: str, ref: str, title: str, files: dict[str, str]) -> dict:
        branch = _unique_branch(kind, project.rsplit("/", 1)[-1])
        await self.gitlab.create_branch(project, branch, ref)
        actions = {path: "update" if await self.gitlab.file_exists(project, path, ref) else "create" for path in files}
        await self.gitlab.commit_files(project, branch, title, files, actions)
        return await self.gitlab.create_merge_request(project, branch, ref, title, "Generated by DevAssist. Please review before merging.")

    async def api_reference(self, project: str, audience: str = "") -> Reply:
        ref = (await self.gitlab.get_project(project))["default_branch"]
        tree = await self.gitlab.list_tree(project, ref)
        source = [p for p in tree if p.endswith(SOURCE_EXTENSIONS) and not is_safe_test_path(p) and not p.endswith("__init__.py")]
        chosen = [p for p in source if _API_FILE.search(p)] or source
        if not chosen:
            raise AgentRefusal("I found no source files to document.")
        contents = "\n\n".join([f"### {p}\n{await self.gitlab.get_file(project, p, ref)}" for p in chosen[:12]])
        audience_line = f"\nAudience: {audience}" if audience else ""
        document = await self.llm.ask("docs", instructions("docs"), f"Write the API reference for {project}.{audience_line}\n\nSource code:\n{contents}")
        fenced = _FENCE.match(document.strip())
        document = fenced.group(1) if fenced else document

        files = {"docs/api.md": document.rstrip() + "\n"}
        if "README.md" in tree:
            readme = await self.gitlab.get_file(project, "README.md", ref)
            if "docs/api.md" not in readme:
                files["README.md"] = readme.rstrip() + "\n\n## API\n\nSee the [API reference](docs/api.md).\n"
        merge_request = await self._open_mr(project, "docs", ref, f"docs: API reference for {project.rsplit('/', 1)[-1]}", files)
        return Reply(f"I documented {len(chosen)} source file(s) in `docs/api.md`" + (" and linked it from the README." if "README.md" in files else "."),
                     [("Merge request", merge_request["web_url"])])

    async def changelog(self, project: str, target: str | None = None) -> Reply:
        """Built from merged MRs by code alone, so it cannot invent entries."""
        ref = target or (await self.gitlab.get_project(project))["default_branch"]
        merged = await self.gitlab.list_merge_requests(project, state="merged", limit=50)
        merged = [mr for mr in merged if not mr["source_branch"].startswith("ai/")]
        if not merged:
            raise AgentRefusal("There are no merged MRs to build a changelog from.")
        today = datetime.now(timezone.utc).date().isoformat()
        section = f"## {today}\n\n" + "\n".join(f"- {mr['title']} (!{mr['iid']})" for mr in merged) + "\n"
        existing = ""
        if await self.gitlab.file_exists(project, "CHANGELOG.md", ref):
            existing = await self.gitlab.get_file(project, "CHANGELOG.md", ref)
        body = existing.replace("# Changelog\n", "", 1).strip()
        merge_request = await self._open_mr(project, "changelog", ref, "docs: update changelog", {"CHANGELOG.md": f"# Changelog\n\n{section}\n{body}".rstrip() + "\n"})
        return Reply(f"Changelog updated with {len(merged)} merged MR(s).", [("Merge request", merge_request["web_url"])])

    async def code_comments(self, project: str, symbol: str = "") -> Reply:
        """Python only: a commit is made only when parsing proves no code changed."""
        ref = (await self.gitlab.get_project(project))["default_branch"]
        tree = await self.gitlab.list_tree(project, ref)
        paths = [p for p in tree if p.endswith(".py") and not is_safe_test_path(p) and not p.endswith("__init__.py")]
        changed: dict[str, str] = {}
        skipped: list[str] = []
        for path in paths[:8]:
            original = await self.gitlab.get_file(project, path, ref)
            if symbol and symbol not in original:
                continue
            answer = await self.llm.ask("docs", instructions("docs_comments"), f"File {path}:\n{original}")
            fenced = _FENCE.match(answer.strip())
            updated = (fenced.group(1) if fenced else answer).rstrip() + "\n"
            try:
                same_code = _code_without_docs(original) == _code_without_docs(updated)
            except SyntaxError:
                same_code = False
            if not same_code:
                skipped.append(path)
            elif updated != original:
                changed[path] = updated
        if not changed:
            raise AgentRefusal("No file could be commented without changing code" + (f" (skipped: {', '.join(skipped)})." if skipped else "."))
        merge_request = await self._open_mr(project, "comments", ref, "docs: add code comments", changed)
        note = f" Skipped because the model changed code: {', '.join(skipped)}." if skipped else ""
        return Reply(f"Added documentation comments to {len(changed)} file(s).{note}", [("Merge request", merge_request["web_url"])])


@dataclass
class IssueDraft:
    project: str
    title: str
    description: str


@dataclass
class TraceResult:
    reply: Reply
    draft: IssueDraft | None = None


class TraceAgent:
    COMPLEX_GROUPS = 3  # more distinct errors than this goes to the stronger model

    def __init__(self, llm: LLM, gitlab: GitLabClient, logs: LogStore, settings: Settings) -> None:
        self.llm, self.gitlab, self.logs, self.settings = llm, gitlab, logs, settings

    async def pipeline(self, project: str, pipeline_id: int) -> TraceResult:
        pipeline = await self.gitlab.get_pipeline(project, pipeline_id)
        if pipeline["status"] != "failed":
            return TraceResult(Reply(f"Pipeline #{pipeline_id} is not red: its status is `{pipeline['status']}`.", [(f"Pipeline #{pipeline_id}", pipeline["web_url"])]))
        jobs = await self.gitlab.list_pipeline_jobs(project, pipeline_id)
        failed = [job for job in jobs if job["status"] == "failed"]
        if not failed:
            return TraceResult(Reply(f"Pipeline #{pipeline_id} failed, but no job is marked as failed.", [(f"Pipeline #{pipeline_id}", pipeline["web_url"])]))
        job = failed[0]
        log = mask(_tail(await self.gitlab.get_job_log(project, job["id"])))
        analysis = await self.llm.ask("trace", instructions("trace"), f"Pipeline #{pipeline_id} of {project} failed in job '{job['name']}' (stage {job['stage']}).\n\nJob log:\n{log}")
        text = f"Pipeline #{pipeline_id} failed at job **{job['name']}** (stage {job['stage']}).\n\n{drop_unverified_urls(analysis, set())}"
        draft = IssueDraft(project, f"Pipeline #{pipeline_id} failed in {job['name']}", f"{analysis}\n\nPipeline: {pipeline['web_url']}")
        return TraceResult(Reply(text + "\n\nSay **open the issue** and I'll file this in GitLab.", [(f"Job {job['name']}", job["web_url"]), (f"Pipeline #{pipeline_id}", pipeline["web_url"])]), draft)

    async def stack_trace(self, text: str) -> TraceResult:
        analysis = await self.llm.ask("trace", instructions("trace"), f"Stack trace:\n{mask(text)}")
        return TraceResult(Reply(drop_unverified_urls(analysis, set())))

    async def incident(self, text: str, project: str | None) -> TraceResult:
        start, end = parse_time_range(text, tz_name=self.settings.timezone)
        known = await self.logs.services()
        lowered = text.lower()
        service = next((name for name in known if name.lower() in lowered), None)
        entries: list[LogEntry] = await self.logs.query(start, end, service)
        groups = group_errors(entries)
        window = f"{start:%Y-%m-%d %H:%M} to {end:%H:%M} {self.settings.timezone}"
        scope = service or "all services"
        if not groups:
            return TraceResult(Reply(f"I found no errors for {scope} between {window} ({len(entries)} log lines checked)."))

        deployments: list[str] = []
        if project:
            for item in await self.gitlab.list_deployments(project):
                when = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                if start.astimezone(timezone.utc).timestamp() - 7200 <= when.timestamp() <= end.astimezone(timezone.utc).timestamp():
                    deployments.append(f"{item['created_at']} {item.get('environment', {}).get('name', '')} {item.get('ref', '')} ({item.get('sha', '')[:8]})")

        summary = "\n".join(f"- {g.count}x, {g.first_seen:%H:%M:%S} to {g.last_seen:%H:%M:%S}: {g.sample}" for g in groups[:10])
        evidence = f"Service: {scope}\nWindow: {window}\nError groups:\n{summary}\n\nRecent deployments:\n" + ("\n".join(deployments) or "(none found)")
        role = "trace_complex" if len(groups) > self.COMPLEX_GROUPS else "trace"
        analysis = drop_unverified_urls(await self.llm.ask(role, instructions("trace"), f"Question: {text}\n\n{evidence}"), set())

        total = sum(g.count for g in groups)
        table = "\n".join(f"- **{g.count}x** `{g.sample[:140]}`" for g in groups[:5])
        body = f"**{total} errors in {len(groups)} group(s)** for {scope}, {window}.\n\n{table}\n\n{analysis}"
        if not project:
            return TraceResult(Reply(body))
        draft = IssueDraft(project, f"{scope}: {groups[0].signature[:80]}", f"{analysis}\n\n{evidence}")
        return TraceResult(Reply(body + "\n\nSay **open the issue** and I'll file this in GitLab."), draft)
