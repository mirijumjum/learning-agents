"""Command line for DevAssist.

    devassist doctor              is everything configured and reachable?
    devassist fake-gitlab         run the local GitLab stand-in, with sample logs
    devassist ask "..."           one request, no Teams needed
    devassist serve               Teams endpoint and GitLab webhooks
    devassist eval                the release test set
    devassist usage               tokens, cost and feedback so far
    devassist package             build the Teams app zip
    devassist notify-teams        send a test card to the Teams Workflows webhook
    devassist simulate-webhook    send a GitLab event to a running `devassist serve`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import httpx

from devassist.audit import AuditLog, RequestContext
from devassist.config import PROFILES, ConfigError, Settings, load_dotenv_file
from devassist.llm import model_name


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


def _notify_teams(args: argparse.Namespace) -> int:
    from devassist.automations import post_to_teams

    url = args.url or _settings(args).teams_notify_url
    if not url:
        raise ValueError("Set TEAMS_NOTIFY_WEBHOOK_URL in .env.devassist, or pass --url.")
    status = asyncio.run(post_to_teams(url, args.title, args.text))
    print(f"Teams accepted the card (HTTP {status}). It can take a few seconds to show up in the channel.")
    return 0


# Events that match the GitLab stand-in's sample data, so the agents have real work to do.
SAMPLE_EVENTS = {
    "pipeline-failed": ("Pipeline Hook", {
        "object_kind": "pipeline", "project": {"path_with_namespace": "acme/payments-api"},
        "object_attributes": {"id": 5512, "status": "failed", "ref": "develop"}, "merge_request": None,
    }),
    "mr-opened": ("Merge Request Hook", {
        "object_kind": "merge_request", "project": {"path_with_namespace": "acme/payments-api"},
        "object_attributes": {"action": "open", "iid": 231, "source_branch": "feature/order-discount", "target_branch": "develop"},
    }),
    "mr-merged": ("Merge Request Hook", {
        "object_kind": "merge_request", "project": {"path_with_namespace": "acme/payments-api"},
        "object_attributes": {"action": "merge", "iid": 231, "source_branch": "feature/order-discount", "target_branch": "develop"},
    }),
}


def _simulate_webhook(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if not settings.gitlab_webhook_secret:
        raise ValueError("Set GITLAB_WEBHOOK_SECRET in .env.devassist (and restart `devassist serve`).")
    event, payload = SAMPLE_EVENTS[args.event]
    headers = {"X-Gitlab-Token": settings.gitlab_webhook_secret, "X-Gitlab-Event": event, "Idempotency-Key": str(uuid.uuid4())}
    response = httpx.post(f"{args.url.rstrip('/')}/webhooks/gitlab", json=payload, headers=headers, timeout=10)
    print(f"HTTP {response.status_code}: {response.text}")
    if response.status_code == 200 and "ignored" in response.text:
        print("Ignored: turn on the matching DEVASSIST_AUTO_* setting and restart `devassist serve`.")
    return 0 if response.status_code in (200, 202) else 1


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
    notify = command("notify-teams", _notify_teams, "Send a test card to the Teams Workflows webhook.")
    notify.add_argument("--url", default="", help="Overrides TEAMS_NOTIFY_WEBHOOK_URL.")
    notify.add_argument("--title", default="DevAssist · test")
    notify.add_argument("--text", default="If you can read this in the channel, the Teams workflow works.")
    simulate = command("simulate-webhook", _simulate_webhook, "Send a sample GitLab event to a running server.")
    simulate.add_argument("event", choices=sorted(SAMPLE_EVENTS))
    simulate.add_argument("--url", default="http://127.0.0.1:3978", help="Where `devassist serve` listens.")
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
