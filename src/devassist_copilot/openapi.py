"""OpenAPI 2.0 (Swagger) files for Power Platform custom connectors, generated from the tool registry.

Two connectors:
- REST: one operation per tool. Used by the agent flow (which cannot call MCP)
  and by topics, and usable as plain connector tools if MCP is not allowed.
- MCP: the single /mcp operation marked with x-ms-agentic-protocol, which is how
  Copilot Studio imports a Streamable HTTP MCP server as a custom connector.

Custom connectors accept OpenAPI 2.0 only, so these are Swagger 2.0, with inline
schemas and no oneOf/anyOf.
"""

from __future__ import annotations

import copy
from typing import Any

from devassist_copilot import __version__
from devassist_copilot.tools import TOOLS

API_KEY_HEADER = "X-API-Key"
REQUESTER_HEADER = "X-DevAssist-Requester"
REST_PREFIX = "/api/tools/"
_DESCRIPTION = ("DevAssist backend for the Copilot Studio agent: GitLab (never merges, deploys or deletes; commits only to ai/* branches), "
                "unit tests run in GitLab CI, documentation merge requests and read-only, masked log analysis.")


def _with_summaries(schema: dict[str, Any]) -> dict[str, Any]:
    """Add x-ms-summary to every property, which Power Platform shows as the field label."""
    schema = copy.deepcopy(schema)
    if schema.get("type") == "object":
        for name, prop in schema.get("properties", {}).items():
            prop.setdefault("x-ms-summary", name[:1].upper() + name[1:])
            schema["properties"][name] = _with_summaries(prop)
    if schema.get("type") == "array" and "items" in schema:
        schema["items"] = _with_summaries(schema["items"])
    schema.pop("additionalProperties", None)
    if not schema.get("required", True):  # Swagger 2.0 requires at least one item
        schema.pop("required")
    return schema


def _base(host: str, title: str, description: str) -> dict[str, Any]:
    return {
        "swagger": "2.0",
        "info": {"title": title, "description": description, "version": __version__,
                 "contact": {"name": "DevAssist owners"}},
        "host": host,
        "basePath": "/",
        "schemes": ["https"],
        "consumes": ["application/json"],
        "produces": ["application/json"],
        "securityDefinitions": {"api_key": {"type": "apiKey", "in": "header", "name": API_KEY_HEADER}},
        "security": [{"api_key": []}],
    }


def build_rest_spec(host: str = "devassist.example.com") -> dict[str, Any]:
    spec = _base(host, "DevAssist", _DESCRIPTION)
    error = {"type": "object", "properties": {"error": {"type": "string", "description": "Why the call was refused", "x-ms-summary": "Error"}}}
    paths: dict[str, Any] = {}
    for tool in TOOLS:
        paths[REST_PREFIX + tool.name] = {"post": {
            "operationId": tool.name,
            "summary": tool.title,
            "description": tool.description,
            "parameters": [
                {"name": "body", "in": "body", "required": True, "schema": _with_summaries(tool.input_schema())},
                {"name": REQUESTER_HEADER, "in": "header", "required": False, "type": "string", "x-ms-summary": "Requester e-mail",
                 "x-ms-visibility": "advanced",
                 "description": "E-mail of the person using the agent. In Copilot Studio set it to System.User.Email, never let the model fill it."},
            ],
            "responses": {
                "200": {"description": "OK", "schema": _with_summaries({"type": "object", "properties": tool.output})},
                "400": {"description": "Refused or failed; the reason is in error", "schema": error},
                "401": {"description": "Missing or wrong API key"},
            },
        }}
    spec["paths"] = paths
    return spec


def build_mcp_spec(host: str = "devassist.example.com") -> dict[str, Any]:
    spec = _base(host, "DevAssist MCP", "DevAssist tools as a Model Context Protocol server (Streamable HTTP). " + _DESCRIPTION)
    spec["paths"] = {"/mcp": {"post": {
        "summary": "DevAssist MCP server",
        "description": "GitLab, CI test loop, documentation and log tools for DevAssist.",
        "x-ms-agentic-protocol": "mcp-streamable-1.0",
        "operationId": "InvokeMCP",
        "responses": {"200": {"description": "Success"}},
    }}}
    return spec


def api_properties(icon_color: str = "#4F6BED") -> dict[str, Any]:
    """apiProperties.json for `paconn create` / `pac connector create`: the API key is asked for when connecting."""
    return {"properties": {
        "connectionParameters": {"api_key": {"type": "securestring", "uiDefinition": {
            "displayName": "API key", "description": "The DevAssist backend API key (from Key Vault).",
            "tooltip": "Sent in the X-API-Key header", "constraints": {"tabIndex": 2, "clearText": False, "required": "true"}}}},
        "iconBrandColor": icon_color,
        "capabilities": [],
        "publisher": "DevAssist owners",
        "stackOwner": "DevAssist owners",
    }}
