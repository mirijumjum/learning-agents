"""devassist-copilot: run the backend, export the connector files, call a tool, estimate cost."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

from devassist.config import ConfigError, Settings, load_dotenv_file
from devassist_copilot import costing
from devassist_copilot.openapi import api_properties, build_mcp_spec, build_rest_spec
from devassist_copilot.server import DEFAULT_PORT, AuthConfig, build_backend, serve
from devassist_copilot.tools import Caller, ToolError

CONNECTOR_DIR = Path("copilot-studio/connector")


def write_connector_files(out: Path, host: str) -> list[Path]:
    files = {
        out / "rest" / "apiDefinition.swagger.json": build_rest_spec(host),
        out / "rest" / "apiProperties.json": api_properties(),
        out / "mcp" / "apiDefinition.swagger.json": build_mcp_spec(host),
        out / "mcp" / "apiProperties.json": api_properties(),
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return list(files)


def usage_summary(audit_path: Path) -> dict[str, object]:
    calls: Counter[str] = Counter()
    failed = 0
    if audit_path.exists():
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("kind") == "tool_call":
                calls[record.get("tool", "?")] += 1
                failed += 0 if record.get("ok") else 1
    total = sum(calls.values())
    return {"tool_calls": dict(calls), "failed": failed,
            "estimated_agent_action_credits": total * costing.AGENT_ACTION,
            "estimated_usd_payg": round(total * costing.AGENT_ACTION * costing.PAYG_USD_PER_CREDIT, 2)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devassist-copilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve_p = sub.add_parser("serve", help="Run the MCP + REST backend")
    serve_p.add_argument("--host", default=os.environ.get("DEVASSIST_COPILOT_HOST", "127.0.0.1"))
    serve_p.add_argument("--port", type=int, default=int(os.environ.get("DEVASSIST_COPILOT_PORT", DEFAULT_PORT)))
    export_p = sub.add_parser("export-connector", help="Write the custom connector OpenAPI files")
    export_p.add_argument("--host", default="devassist.example.com", help="Public host name of the backend")
    export_p.add_argument("--out", type=Path, default=CONNECTOR_DIR)
    call_p = sub.add_parser("call", help="Call one tool locally, as the agent would")
    call_p.add_argument("tool")
    call_p.add_argument("arguments", nargs="?", default="{}", help="JSON object")
    call_p.add_argument("--as", dest="email", default="dev@acme.test")
    sub.add_parser("estimate", help="Monthly Copilot Credits estimate for 5 and 20 developers")
    sub.add_parser("usage", help="Tool calls recorded in the audit log, with their credit cost")
    args = parser.parse_args(argv)

    if args.command == "export-connector":
        for path in write_connector_files(args.out, args.host):
            print(path)
        return 0
    if args.command == "estimate":
        for developers in (5, 20):
            print(json.dumps(costing.estimate(developers).__dict__))
        return 0

    load_dotenv_file()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "usage":
        print(json.dumps(usage_summary(settings.audit_path), indent=2))
        return 0
    if args.command == "call":
        async def run() -> int:
            backend = build_backend(settings)
            try:
                print(json.dumps(await backend.call(args.tool, json.loads(args.arguments), Caller(args.email)), indent=2, ensure_ascii=False))
                return 0
            except ToolError as exc:
                print(f"Refused: {exc}", file=sys.stderr)
                return 1
            finally:
                await backend.gitlab.aclose()
        return asyncio.run(run())
    try:
        asyncio.run(serve(settings, AuthConfig.from_env(), args.host, args.port))
    except (RuntimeError, KeyboardInterrupt) as exc:
        if isinstance(exc, RuntimeError):
            print(str(exc), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
