"""Fail if a changed Python file differs from a base revision in anything but docstrings or comments.

Used by the documentation custom agent (.github/agents/devassist-docs.agent.md), the same check
DevAssist's DocsAgent does in code: the syntax tree without docstrings must stay identical.

    python scripts/check_doc_only_changes.py            # compare the working tree with HEAD
    python scripts/check_doc_only_changes.py main       # compare with another revision
"""

from __future__ import annotations

import ast
import subprocess
import sys


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def code_only(source: str) -> str:
    """Return a dump of the syntax tree with docstrings removed (comments never reach the tree)."""
    return ast.dump(_strip_docstrings(ast.parse(source)), include_attributes=False)


def only_docs_changed(before: str, after: str) -> bool:
    return code_only(before) == code_only(after)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "HEAD"
    changed = [p for p in _git("diff", "--name-only", base, "--", "*.py").splitlines() if p]
    offenders = []
    for path in changed:
        try:
            before = _git("show", f"{base}:{path}")
        except subprocess.CalledProcessError:
            offenders.append(f"{path} (new file)")
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                after = fh.read()
        except FileNotFoundError:
            offenders.append(f"{path} (deleted)")
            continue
        if not only_docs_changed(before, after):
            offenders.append(path)
    for path in offenders:
        print(f"code changed, not only docs: {path}")
    if not offenders:
        print(f"OK: {len(changed)} Python file(s) changed, docstrings/comments only.")
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
