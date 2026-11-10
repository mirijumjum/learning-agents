"""CI guard for branches written by AI agents (GitLab Duo flows).

Runs in the target project's pipeline (standard library only) and fails it when:

1. an AI service account pushed to a branch that does not start with ``ai/``;
2. an ``ai/*`` branch deletes files or touches protected paths
   (``.gitlab-ci.yml``, ``.gitlab/``, ``AGENTS.md``, ``CODEOWNERS``, ``skills/``, ``.agents/``);
3. an ``ai/tests-*`` branch changes anything that is not a test file;
4. an ``ai/docs-*`` branch changes anything but documentation, or changes a Python
   file beyond its docstrings and comments (the syntax trees are compared).

Prompts ask the agents to follow these rules. This job checks that they did.

Environment (all set by GitLab CI): ``CI_COMMIT_REF_NAME``, ``GITLAB_USER_LOGIN``,
``CI_MERGE_REQUEST_DIFF_BASE_SHA`` (merge request pipelines), ``CI_DEFAULT_BRANCH``.
``AI_BOT_PATTERN`` (regex) names the AI accounts.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass

AI_PREFIX = "ai/"
DEFAULT_BOT_PATTERN = r"^(ai-.+|duo-.+|gitlabduo)$"
PROTECTED = (".gitlab-ci.yml", ".gitlab/", "AGENTS.md", "CODEOWNERS", "skills/", ".agents/")
TEST_DIRS = ("tests/", "test/", "spec/", "__tests__/")
TEST_NAMES = (
    "test_*.py", "*_test.py", "conftest.py", "*_test.go", "*.test.js", "*.test.ts", "*.test.jsx", "*.test.tsx",
    "*.spec.js", "*.spec.ts", "*Test.java", "*Tests.java", "*Tests.cs", "*Test.cs", "*_spec.rb",
)
DOC_DIRS = ("docs/", "doc/")
DOC_NAMES = ("*.md", "*.rst", "*.adoc", "CHANGELOG*", "README*")


@dataclass(frozen=True)
class Change:
    status: str  # A, M or D (git diff --name-status --no-renames)
    path: str


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _in_dirs(path: str, dirs: Iterable[str]) -> bool:
    return any(path.startswith(d) or f"/{d}" in f"/{path}" for d in dirs)


def is_test_file(path: str) -> bool:
    return _in_dirs(path, TEST_DIRS) or any(fnmatch.fnmatch(_name(path), p) for p in TEST_NAMES)


def is_doc_file(path: str) -> bool:
    return _in_dirs(path, DOC_DIRS) or any(fnmatch.fnmatch(_name(path), p) for p in DOC_NAMES)


def is_protected(path: str) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) or _name(path) == p for p in PROTECTED)


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return tree


def same_code(before: str, after: str) -> bool:
    """True when two Python sources differ only in comments, docstrings and formatting."""
    try:
        return ast.dump(_strip_docstrings(ast.parse(before))) == ast.dump(_strip_docstrings(ast.parse(after)))
    except SyntaxError:
        return False


def check(
    branch: str,
    pusher: str,
    changes: list[Change],
    read_before: Callable[[str], str],
    read_after: Callable[[str], str],
    bot_pattern: str = DEFAULT_BOT_PATTERN,
) -> list[str]:
    """All rule violations; an empty list means the pipeline may go on."""
    problems: list[str] = []
    is_bot = bool(pusher) and re.match(bot_pattern, pusher, re.IGNORECASE) is not None
    if is_bot and not branch.startswith(AI_PREFIX):
        problems.append(f"AI account '{pusher}' pushed to '{branch}': AI accounts may only push to {AI_PREFIX}* branches.")
    if not branch.startswith(AI_PREFIX):
        return problems

    kind = branch[len(AI_PREFIX):].split("-", 1)[0]
    for change in changes:
        path = change.path
        if change.status == "D":
            problems.append(f"{path}: AI branches may not delete files.")
            continue
        if is_protected(path):
            problems.append(f"{path}: protected path, AI branches may not change it.")
            continue
        if kind == "tests" and not is_test_file(path):
            problems.append(f"{path}: an ai/tests-* branch may only add or change test files.")
        elif kind == "docs" and not is_doc_file(path):
            if path.endswith(".py") and change.status == "M":
                if not same_code(read_before(path), read_after(path)):
                    problems.append(f"{path}: an ai/docs-* branch changed code, not only docstrings and comments.")
            else:
                problems.append(f"{path}: an ai/docs-* branch may only change documentation and Python docstrings.")
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _base_sha(env: dict[str, str]) -> str:
    if env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA"):
        return env["CI_MERGE_REQUEST_DIFF_BASE_SHA"]
    default = env.get("CI_DEFAULT_BRANCH", "main")
    _git("fetch", "--quiet", "origin", default)
    return _git("merge-base", "HEAD", f"origin/{default}").strip()


def main(env: dict[str, str] | None = None) -> int:
    env = dict(os.environ if env is None else env)
    branch = env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME") or env.get("CI_COMMIT_REF_NAME", "")
    pusher = env.get("GITLAB_USER_LOGIN", "")
    bot_pattern = env.get("AI_BOT_PATTERN") or DEFAULT_BOT_PATTERN
    changes: list[Change] = []
    base = ""
    if branch.startswith(AI_PREFIX):
        base = _base_sha(env)
        for line in _git("diff", "--name-status", "--no-renames", f"{base}...HEAD").splitlines():
            status, _, path = line.partition("\t")
            changes.append(Change(status[:1], path))

    def read_before(path: str) -> str:
        return _git("show", f"{base}:{path}")

    def read_after(path: str) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    problems = check(branch, pusher, changes, read_before, read_after, bot_pattern)
    for problem in problems:
        print(f"ai-branch-guard: {problem}")
    if not problems:
        print(f"ai-branch-guard: ok ({branch}, {len(changes)} changed file(s), pushed by {pusher or 'unknown'}).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
