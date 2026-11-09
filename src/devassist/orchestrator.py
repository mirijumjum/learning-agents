"""DevAssist: receives a request, picks a specialist, returns one reply."""

from __future__ import annotations

import re
from dataclasses import dataclass

from devassist.audit import AuditLog, RequestContext, current_request
from devassist.config import Settings
from devassist.gitlab import GitLabClient, GitLabError
from devassist.llm import LLM, LLMTimeout
from devassist.logstore import LogStore
from devassist.masking import mask
from devassist.specialists import AgentRefusal, DocsAgent, GitAgent, IssueDraft, Reply, TestAgent, TraceAgent, instructions

ROUTES = ("test", "docs", "git", "trace", "help")
HELP_TEXT = (
    "I'm **DevAssist**. I can:\n\n"
    "- **Write and run tests**: \"Generate unit tests for `OrderService` in MR !231 of `payments-api`\"\n"
    "- **Write documentation**: \"Document the public endpoints of `customer-portal` and open an MR\"\n"
    "- **Work with GitLab**: \"Create a branch `feature/x` from `develop` in `payments-api`\"\n"
    "- **Find out why something failed**: \"Why is pipeline #5512 red?\"\n\n"
    "I never merge, deploy or delete. Changes land on a branch or MR for a person to review.\n"
    "Tell me how I did with: `feedback good` or `feedback bad <why>`."
)
_STACK_TRACE = re.compile(r"Traceback \(most recent|^\s+at [\w.$]+\(.*:\d+\)|Exception in thread", re.M)
_MR = re.compile(r"(?:!|\bMR\s*#?)(\d+)", re.I)
_PIPELINE = re.compile(r"\bpipeline\s*#?(\d+)", re.I)
_BACKTICK = re.compile(r"`([^`]+)`")
_OPEN_ISSUE = re.compile(r"\b(open|create|file|abre|crea)\b.*\bissue\b", re.I)
_FEEDBACK = re.compile(r"^\s*feedback\s+(good|bad)\b\s*(.*)$", re.I | re.S)
_ROUTE_WORD = re.compile(r"\b(" + "|".join(ROUTES) + r")\b")


def keyword_route(text: str) -> str:
    """Used when the model's routing answer can't be understood."""
    lowered = text.lower()
    if _STACK_TRACE.search(text) or re.search(r"\b(why|por qu[eé])\b.*\b(fail|red|error|crash|fall|roj)|\broot cause\b", lowered):
        return "trace"
    if re.search(r"\b(unit )?tests?\b|\bcoverage\b", lowered) and re.search(r"\b(generate|write|add|create|genera|escribe)\b", lowered):
        return "test"
    if re.search(r"\b(document|docs?|readme|changelog|documenta|comments?|comentarios?|docstrings?)\b", lowered):
        return "docs"
    if re.search(r"\b(branch|rama|merge request|mrs?|issue|pipeline)\b", lowered):
        return "git"
    return "help"


@dataclass
class _Session:
    draft: IssueDraft | None = None


class DevAssist:
    def __init__(self, settings: Settings, audit: AuditLog, llm: LLM, gitlab: GitLabClient, logs: LogStore) -> None:
        self.settings, self.audit, self.llm, self.gitlab = settings, audit, llm, gitlab
        self.git = GitAgent(llm, gitlab, settings)
        self.test = TestAgent(llm, gitlab, settings)
        self.docs = DocsAgent(llm, gitlab, settings)
        self.trace = TraceAgent(llm, gitlab, logs, settings)
        self._sessions: dict[str, _Session] = {}

    async def route(self, text: str) -> str:
        if _STACK_TRACE.search(text):
            return "trace"
        answer = await self.llm.ask("router", instructions("router"), text)
        words = _ROUTE_WORD.findall(answer.lower())
        return words[-1] if words else keyword_route(text)

    async def _find_project(self, text: str) -> str | None:
        """A project mentioned by name, in backticks, or the configured default."""
        paths = await self.gitlab.list_project_paths()
        for path in paths:
            name = path.rsplit("/", 1)[-1]
            if re.search(rf"(?<![\w-]){re.escape(path)}(?![\w-])|(?<![\w/-]){re.escape(name)}(?![\w-])", text):
                return path
        for candidate in _BACKTICK.findall(text):
            try:
                return await self.gitlab.resolve_project(candidate)
            except GitLabError:
                continue
        if self.settings.default_project:
            return await self.gitlab.resolve_project(self.settings.default_project)
        return None

    @staticmethod
    def _symbol(text: str, project: str) -> str:
        name = project.rsplit("/", 1)[-1]
        return next((token for token in _BACKTICK.findall(text) if token not in (name, project)), "")

    async def _need_project(self, text: str) -> str:
        project = await self._find_project(text)
        if project is None:
            raise AgentRefusal("Which project do you mean? Name it, for example `payments-api`.")
        return project

    async def _dispatch(self, route: str, text: str, context: RequestContext) -> Reply:
        session = self._sessions.setdefault(context.conversation_id, _Session())

        if route == "test":
            project = await self._need_project(text)
            mr = _MR.search(text)
            return (await self.test.run(project, mr_iid=int(mr.group(1)) if mr else None, symbol=self._symbol(text, project))).reply

        if route == "docs":
            project = await self._need_project(text)
            lowered = text.lower()
            if "changelog" in lowered:
                return await self.docs.changelog(project)
            if re.search(r"\bcomments?\b|\bcomentarios?\b|docstring", lowered):
                return await self.docs.code_comments(project, self._symbol(text, project))
            return await self.docs.api_reference(project)

        if route == "git":
            return await self.git.run(text, context)

        if route == "trace":
            pipeline = _PIPELINE.search(text)
            if pipeline:
                result = await self.trace.pipeline(await self._need_project(text), int(pipeline.group(1)))
            elif _STACK_TRACE.search(text):
                result = await self.trace.stack_trace(text)
            else:
                result = await self.trace.incident(text, await self._find_project(text))
            session.draft = result.draft
            return result.reply

        return Reply(HELP_TEXT)

    async def handle(self, text: str, context: RequestContext) -> Reply:
        token = current_request.set(context)
        self.audit.event("request_start", text=mask(text)[:500])
        route, ok = "", True
        try:
            feedback = _FEEDBACK.match(text)
            if feedback:
                route = "feedback"
                self.audit.event("feedback", rating=feedback.group(1).lower(), comment=mask(feedback.group(2))[:500])
                return Reply("Thanks, noted.")

            session = self._sessions.get(context.conversation_id)
            if session and session.draft and _OPEN_ISSUE.search(text):
                route = "open_issue"
                draft, session.draft = session.draft, None
                issue = await self.gitlab.create_issue(draft.project, draft.title, draft.description)
                return Reply(f"Issue #{issue['iid']} created.", [(f"Issue #{issue['iid']}", issue["web_url"])])

            route = await self.route(text)
            return await self._dispatch(route, text, context)
        except (AgentRefusal, LLMTimeout) as exc:
            return Reply(str(exc))
        except GitLabError as exc:
            ok = False
            self.audit.event("error", error=str(exc)[:500])
            return Reply(f"I couldn't complete that because of a GitLab problem: {exc}")
        except Exception as exc:
            ok = False
            self.audit.event("error", error=f"{type(exc).__name__}: {exc}"[:500])
            raise
        finally:
            self.audit.event("request_end", route=route, ok=ok)
            current_request.reset(token)
