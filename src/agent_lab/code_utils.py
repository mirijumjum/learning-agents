"""Helpers to turn free-form LLM answers into usable Python source."""

from __future__ import annotations

import ast
import re

_FENCE = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_python_code(text: str) -> str:
    """Return the code inside the largest ```python fenced block, or the raw text if there is none."""
    blocks = _FENCE.findall(text)
    code = max(blocks, key=len) if blocks else text
    return code.strip() + "\n"


def describe_public_api(source: str) -> str:
    """Summarise the public, top-level functions of a module as signatures + docstrings.

    The tester only gets this summary (not the implementation), so it writes tests
    from the *specification* instead of copying whatever bugs the coder introduced.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"# The module does not parse: {exc}"

    parts: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            doc = ast.get_docstring(node)
            body = f'    """{doc}"""' if doc else "    ..."
            parts.append(f"{prefix} {node.name}({ast.unparse(node.args)}){returns}:\n{body}")
    return "\n\n".join(parts) if parts else "# No public functions found."
