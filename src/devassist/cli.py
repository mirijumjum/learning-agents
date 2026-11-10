"""Command line for DevAssist.

    devassist doctor              is everything configured and reachable?
    devassist fake-gitlab         run the local GitLab stand-in, with sample logs
    devassist ask "..."           one request, no Teams needed
    devassist serve               Teams endpoint and GitLab webhooks
    devassist eval                the release test set
    devassist usage               tokens, cost and feedback so far
    devassist package             build the Teams app zip
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import httpx

from devassist.audit import AuditLog, RequestContext
from devassist.config import PROFILES, ConfigError, Settings, load_dotenv_file
from devassist.llm import model_name, server_url


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "profile", None):
        settings = replace(settings, profile=args.profile)
        settings.validate()
    return settings


def _doctor(args: argparse.Namespace) -> int:
    settings = _settings(args)
    print(f"Profile: {settings.profile}")
    print(f"Models:  strong={model_name(settings, 'strong')}  fast={model_name(settings, 'fast')}")
    problems = 0

    def report(ok: bool, label: str, hint: str = "") -> None:
        nonlocal problems
        problems += 0 if ok else 1
        print(f"  [{'ok' if ok else 'FAIL'}] {label}" + (f": {hint}" if hint and not ok else ""))

    if settings.profile == "ollama":
        try:
            tags = httpx.get(settings.ollama_base_url.removesuffix("/v1") + "/api/tags", timeout=5).json()
            names = {m["name"] for m in tags["models"]}
            report(True, "Ollama is running")
            for model in (settings.ollama_model_strong, settings.ollama_model_fast):
                report(model in names, f"model {model}", f"run: ollama pull {model}")
        except httpx.HTTPError:
            report(False, "Ollama is running", "start the Ollama app")
    if settings.profile == "openai-compatible":
        headers = {"Authorization": f"Bearer {settings.openai_compat_api_key}"} if settings.openai_compat_api_key else {}
        for tier in ("strong", "fast"):
            url, model = server_url(settings, tier), model_name(settings, tier)
            try:
                response = httpx.get(f"{url}/models", headers=headers, timeout=10)
                if response.status_code != 200:
                    report(False, f"model server at {url}", f"HTTP {response.status_code}")
                    continue
                served = {m.get("id") for m in response.json().get("data", [])}
                report(True, f"model server at {url}")
                report(model in served, f"model {model}", f"the server offers: {', '.join(sorted(filter(None, served))) or 'nothing'}")
            except (httpx.HTTPError, ValueError):
                report(False, f"model server at {url}", "is it running and reachable?")
    try:
        response = httpx.get(f"{settings.gitlab_url}/api/v4/projects", headers={"PRIVATE-TOKEN": settings.gitlab_token}, params={"simple": "true", "per_page": 1}, timeout=10)
        report(response.status_code == 200, f"GitLab at {settings.gitlab_url}", f"HTTP {response.status_code}")
    except httpx.HTTPError:
        report(False, f"GitLab at {settings.gitlab_url}", "for the local stand-in, run: devassist fake-gitlab")
    if settings.log_store == "jsonl":
        report(settings.log_jsonl_path.exists(), f"log file {settings.log_jsonl_path}", "run: devassist fake-gitlab (it creates sample logs)")
    print("All good." if not problems else f"{problems} problem(s).")
    return 1 if problems else 0


def _fake_gitlab(args: argparse.Namespace) -> int:
    from devassist.dev_gitlab import seed_sample_logs, serve_fake_gitlab

    settings = _settings(args)
    count = seed_sample_logs(settings.log_jsonl_path)
    print(f"Wrote {count} sample log lines to {settings.log_jsonl_path}")
    print(f"Local GitLab stand-in on http://127.0.0.1:{args.port} (Ctrl+C to stop)", flush=True)
    try:
        asyncio.run(serve_fake_gitlab("127.0.0.1", args.port))
    except KeyboardInterrupt:
        pass
    return 0


def _ask(args: argparse.Namespace) -> int:
    from devassist.runtime import build_devassist

    settings = _settings(args)

    async def run() -> str:
        assistant = build_devassist(settings)
        try:
            context = RequestContext(user=args.user, email=f"{args.user}@local.test", conversation_id="cli", source="cli")
            return (await assistant.handle(args.request, context)).render()
        finally:
            await assistant.gitlab.aclose()

    print(asyncio.run(run()))
    return 0


def _serve(args: argparse.Namespace) -> int:
    from aiohttp import web

    from devassist.runtime import build_devassist
    from devassist.server import check_bind_is_safe, create_app, is_authenticated_mode

    settings = _settings(args)
    check_bind_is_safe(args.host)
    print(f"DevAssist on http://{args.host}:{args.port}  profile={settings.profile}  authenticated={is_authenticated_mode()}", flush=True)
    web.run_app(create_app(build_devassist(settings)), host=args.host, port=args.port, print=None)
    return 0


def _eval(args: argparse.Namespace) -> int:
    from devassist.evals import run_evals

    settings = _settings(args)

    def progress(result) -> None:
        print(f"  [{'pass' if result.passed else 'FAIL'}] {result.id}" + (f": {result.detail}" if result.detail else ""), flush=True)

    print(f"Running evals with profile={settings.profile}", flush=True)
    results = asyncio.run(run_evals(settings, args.filter, progress))
    passed = sum(r.passed for r in results)
    print(f"{passed}/{len(results)} passed")
    return 0 if results and passed == len(results) else 1


def _usage(args: argparse.Namespace) -> int:
    print(json.dumps(AuditLog(_settings(args).audit_path).summary(), indent=2))
    return 0


def _package(args: argparse.Namespace) -> int:
    from devassist.teams_package import build_manifest, write_package

    manifest = build_manifest(args.bot_id, args.developer, args.website, args.privacy_url, args.terms_url, args.version)
    print(f"Wrote {write_package(manifest, Path(args.output))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devassist", description="Development agents for tests, docs, GitLab and root-cause analysis.")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--profile", choices=PROFILES, help="Overrides DEVASSIST_PROFILE.")
        item.set_defaults(func=handler)
        return item

    command("doctor", _doctor, "Check configuration and connections.")
    command("fake-gitlab", _fake_gitlab, "Run the local GitLab stand-in.").add_argument("--port", type=int, default=8929)
    ask = command("ask", _ask, "Send one request.")
    ask.add_argument("request")
    ask.add_argument("--user", default="dev")
    serve = command("serve", _serve, "Run the Teams endpoint and GitLab webhooks.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=3978)
    command("eval", _eval, "Run the release test set.").add_argument("--filter", default="", help="Only cases whose id contains this.")
    command("usage", _usage, "Show tokens, cost and feedback.")
    package = command("package", _package, "Build the Teams app package.")
    package.add_argument("--bot-id", required=True)
    package.add_argument("--developer", required=True)
    package.add_argument("--website", required=True)
    package.add_argument("--privacy-url", required=True)
    package.add_argument("--terms-url", required=True)
    package.add_argument("--version", default="1.0.0")
    package.add_argument("--output", default="dist/devassist-teams.zip")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # model replies contain characters Windows consoles can't encode by default
    load_dotenv_file()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
