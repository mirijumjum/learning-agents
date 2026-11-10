"""devassist-actions serve | package | validate"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from devassist.config import ConfigError, Settings, load_dotenv_file

PROJECT_DIR = Path(__file__).resolve().parents[2]  # m365-copilot/


def _serve(args: argparse.Namespace) -> int:
    from aiohttp import web

    from devassist_actions.api import create_app
    from devassist_actions.auth import AuthConfig, Authenticator, check_bind_is_safe

    load_dotenv_file()
    settings = Settings.from_env()
    auth = AuthConfig.from_env()
    check_bind_is_safe(args.host, auth)
    print(f"DevAssist actions API on http://{args.host}:{args.port}/api  GitLab={settings.gitlab_url}  authenticated={auth.enabled}", flush=True)
    web.run_app(create_app(settings, Authenticator(auth)), host=args.host, port=args.port, print=None)
    return 0


def _package(args: argparse.Namespace) -> int:
    from devassist_actions.package import build_package

    output = build_package(PROJECT_DIR, args.env, Path(args.output) if args.output else None)
    print(f"Wrote {output}")
    return 0


def _validate(args: argparse.Namespace) -> int:
    from devassist_actions.package import validate_project

    problems = validate_project(PROJECT_DIR, args.env, schema_dir=Path(args.schemas) if args.schemas else None, download=not args.offline)
    for problem in problems:
        print(f"  - {problem}")
    print("Manifests valid." if not problems else f"{len(problems)} problem(s).")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devassist-actions", description="DevAssist actions API for Microsoft 365 Copilot")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the actions API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=3982)
    serve.set_defaults(handler=_serve)
    package = sub.add_parser("package", help="Build the app package zip with values from env/.env.<env>")
    package.add_argument("--env", default="local")
    package.add_argument("--output")
    package.set_defaults(handler=_package)
    validate = sub.add_parser("validate", help="Validate manifests against Microsoft's official JSON schemas")
    validate.add_argument("--env", default="local")
    validate.add_argument("--schemas", help="Folder with already downloaded schemas")
    validate.add_argument("--offline", action="store_true", help="Do not download missing schemas")
    validate.set_defaults(handler=_validate)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (ConfigError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
