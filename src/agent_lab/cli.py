"""Command line interface.

    agent-lab run "a function that ..."   run the workflow
    agent-lab graph                        print the workflow graph (Mermaid or Graphviz DOT)
    agent-lab providers                    list the LLM providers and their env vars
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from agent_framework import WorkflowEvent, WorkflowViz

from agent_lab.messages import CodeDraft, FeatureRequest, FinalResult, GeneratedTests, RunReport
from agent_lab.mock_client import DEMO_PROMPTS, MockChatClient, find_scenario
from agent_lab.providers import PROVIDERS, ProviderConfigError, make_client_factory
from agent_lab.workflow import build_workflow

# ---------------------------------------------------------------- pretty output

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str: return _c(t, "1")
def dim(t: str) -> str: return _c(t, "2")
def green(t: str) -> str: return _c(t, "32")
def red(t: str) -> str: return _c(t, "31")
def cyan(t: str) -> str: return _c(t, "36")
def yellow(t: str) -> str: return _c(t, "33")


def _indent(text: str, prefix: str = "    │ ") -> str:
    return "\n".join(prefix + line for line in text.rstrip().splitlines())


ROLE_LABELS = {
    "coder": "🧑‍💻 coder",
    "tester": "🧪 tester",
    "runner": "🏃 runner",
    "quality_gate": "🚦 quality_gate",
}


class EventPrinter:
    """Turns the workflow's event stream into readable console output."""

    def __init__(self, verbose: bool, trace: bool) -> None:
        self.verbose = verbose
        self.trace = trace

    def handle(self, event: WorkflowEvent) -> None:
        if self.trace:
            src = getattr(event, "executor_id", None)
            print(dim(f"  [event] {event.type}" + (f" ({src})" if src else "")))

        if event.type == "executor_invoked":
            print(f"\n{bold(ROLE_LABELS.get(event.executor_id, event.executor_id))} {dim('working...')}")
        elif event.type == "executor_completed":
            for message in event.data or []:
                self._show_message(message)
        elif event.type == "executor_failed":
            print(red(f"  ✘ executor {event.executor_id} failed: {event.data}"))
        elif event.type in ("failed", "error"):
            print(red(f"\n✘ workflow error: {event.data}"))
        elif event.type == "warning":
            print(yellow(f"  ! {event.data}"))

    def _show_message(self, message: object) -> None:
        if isinstance(message, CodeDraft):
            names = re.findall(r"^(?:async )?def (\w+)", message.public_api, re.M)
            print(f"  wrote {message.path} ({len(message.source.splitlines())} lines) → exposes: {', '.join(names) or '-'}")
            if self.verbose:
                print(_indent(message.source))
        elif isinstance(message, GeneratedTests):
            print(f"  wrote {message.path} with {message.count} test function(s)")
            if self.verbose:
                print(_indent(message.source))
        elif isinstance(message, RunReport):
            status = green("✔ tests passed") if message.passed else red(f"✘ tests failed (exit code {message.exit_code})")
            print(f"  {status}")
            print(_indent(message.summary, "    » "))
            if self.verbose:
                print(_indent(message.output))
        elif isinstance(message, FeatureRequest):
            print(yellow(f"  ↺ sending feedback to the coder: attempt {message.attempt}/{message.max_attempts}"))
        elif isinstance(message, FinalResult):
            pass  # printed at the end
        else:
            print(f"  → {message!r}")


# -------------------------------------------------------------------- commands


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"


async def _run(args: argparse.Namespace) -> int:
    description = args.description or DEMO_PROMPTS["palindrome"]

    if args.provider == "mock" and find_scenario(description) is None:
        print(red("The mock provider only has scripts for these demos:"))
        for prompt in DEMO_PROMPTS.values():
            print(f'  agent-lab run "{prompt}"')
        print("Use --provider openai|azure|anthropic for anything else (see: agent-lab providers).")
        return 2

    try:
        client_factory = make_client_factory(args.provider, args.model)
    except ProviderConfigError as exc:
        print(red(f"Provider '{args.provider}' is not configured: {exc}"))
        return 2

    workspace = Path(args.workspace) / f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(description)}"
    workspace.mkdir(parents=True, exist_ok=True)
    workflow = build_workflow(client_factory, workspace)

    print(bold("Feature: ") + description)
    print(dim(f"provider={args.provider}  max_attempts={args.max_attempts}  workspace={workspace}"))

    printer = EventPrinter(verbose=args.verbose, trace=args.trace)
    request = FeatureRequest(description=description, max_attempts=args.max_attempts)

    final: FinalResult | None = None
    async for event in workflow.run(request, stream=True):
        printer.handle(event)
        if event.type == "output" and isinstance(event.data, FinalResult):
            final = event.data

    print()
    if final is None:
        print(red("The workflow finished without producing a result."))
        return 1
    if final.success:
        print(green(bold(f"✔ Done in {final.attempts} attempt(s).")))
    else:
        print(red(bold(f"✘ Tests still failing after {final.attempts} attempt(s).")))
    print(f"  code:  {final.solution_path}\n  tests: {final.tests_path}")
    return 0 if final.success else 1


def _graph(args: argparse.Namespace) -> int:
    # The graph's shape does not depend on the LLM, so the mock is enough to draw it.
    workflow = build_workflow(lambda role: MockChatClient(role), Path(args.workspace))
    viz = WorkflowViz(workflow)
    print(viz.to_mermaid() if args.format == "mermaid" else viz.to_digraph())
    return 0


def _providers(_: argparse.Namespace) -> int:
    for name, help_text in PROVIDERS.items():
        print(f"{bold(name):<12} {help_text}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-lab", description="Learn Microsoft Agent Framework with a coder/tester/runner workflow.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the write → test → run workflow.")
    run.add_argument("description", nargs="?", help="What function to build (defaults to a palindrome demo).")
    run.add_argument("-p", "--provider", choices=list(PROVIDERS), default="mock")
    run.add_argument("-m", "--model", help="Model / deployment name (overrides env vars).")
    run.add_argument("-n", "--max-attempts", type=int, default=3)
    run.add_argument("-w", "--workspace", default="workspace", help="Where generated files are written.")
    run.add_argument("-v", "--verbose", action="store_true", help="Show generated code, tests and pytest output.")
    run.add_argument("--trace", action="store_true", help="Also print every raw workflow event.")
    run.set_defaults(func=lambda a: asyncio.run(_run(a)))

    graph = sub.add_parser("graph", help="Print the workflow graph.")
    graph.add_argument("-f", "--format", choices=["mermaid", "dot"], default="mermaid")
    graph.add_argument("-w", "--workspace", default="workspace")
    graph.set_defaults(func=_graph)

    providers = sub.add_parser("providers", help="List LLM providers and the env vars they need.")
    providers.set_defaults(func=_providers)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
