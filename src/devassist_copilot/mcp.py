"""A small Model Context Protocol server over Streamable HTTP, the transport Copilot Studio supports.

Stateless: every POST carries one JSON-RPC message and gets a JSON reply, which
the Streamable HTTP transport allows. Only tools are offered (no resources or
prompts), which is all the agent needs.
"""

from __future__ import annotations

import json
from typing import Any

from devassist_copilot import __version__
from devassist_copilot.tools import TOOLS, Backend, Caller, ToolError

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INSTRUCTIONS = (
    "DevAssist tools for GitLab, unit tests that run in GitLab CI, documentation merge requests and read-only log analysis. "
    "Results, counts and links come from these tools; never invent them."
)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def tool_descriptors() -> list[dict[str, Any]]:
    return [{
        "name": tool.name,
        "title": tool.title,
        "description": tool.description,
        "inputSchema": tool.input_schema(),
        "annotations": {"title": tool.title, "readOnlyHint": tool.read_only, "destructiveHint": False,
                        "idempotentHint": tool.read_only, "openWorldHint": False},
    } for tool in TOOLS]


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


async def handle_message(backend: Backend, raw: bytes, caller: Caller) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications, which get HTTP 202."""
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(None, PARSE_ERROR, "Parse error")
    if isinstance(message, list):
        return _error(None, INVALID_REQUEST, "Batches are not supported")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "Invalid request")

    method, params = message["method"], message.get("params") or {}
    if "id" not in message:  # notification, e.g. notifications/initialized
        return None
    message_id = message["id"]

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return _result(message_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "devassist", "title": "DevAssist", "version": __version__},
            "instructions": SERVER_INSTRUCTIONS,
        })
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": tool_descriptors()})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _error(message_id, INVALID_PARAMS, "params.name is required")
        if name not in {tool.name for tool in TOOLS}:
            return _error(message_id, INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            data = await backend.call(name, params.get("arguments") or {}, caller)
        except ToolError as exc:
            # A tool failure is a result the model reads and explains, not a protocol error.
            return _result(message_id, {"content": [{"type": "text", "text": f"ERROR: {exc}"}], "isError": True})
        return _result(message_id, {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                                    "structuredContent": data, "isError": False})
    return _error(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")
