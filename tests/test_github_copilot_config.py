"""Checks the GitHub Copilot configuration against the rules in GitHub Docs (read 2026-11-10).

- Custom agents: https://docs.github.com/en/copilot/reference/custom-agents-configuration
- Instructions: https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions
- Setup steps: https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-environment
- Rulesets: https://docs.github.com/en/rest/repos/rules
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
GH = ROOT / ".github"

AGENT_KEYS = {"name", "description", "target", "tools", "model", "disable-model-invocation",
              "user-invocable", "mcp-servers", "metadata"}
TOOL_ALIASES = {"execute", "shell", "bash", "powershell", "read", "notebookread", "edit", "multiedit", "write",
                "notebookedit", "search", "grep", "glob", "agent", "custom-agent", "task", "web", "websearch",
                "webfetch", "todo", "todowrite"}
SETUP_JOB_KEYS = {"steps", "permissions", "runs-on", "services", "snapshot", "timeout-minutes"}
RULE_TYPES = {"creation", "update", "deletion", "required_linear_history", "merge_queue", "required_deployments",
              "required_signatures", "pull_request", "required_status_checks", "non_fast_forward",
              "commit_message_pattern", "commit_author_email_pattern", "committer_email_pattern",
              "branch_name_pattern", "tag_name_pattern", "workflows", "code_scanning", "code_quality",
              "code_coverage", "copilot_code_review", "license_compliance_scanning", "file_path_restriction",
              "max_file_path_length", "file_extension_restriction", "max_file_size"}


def split_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.S)
    assert match, f"{path} has no YAML frontmatter"
    return yaml.safe_load(match.group(1)), match.group(2)


def load_workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in data:  # PyYAML (YAML 1.1) reads the key `on` as boolean True
        data["on"] = data.pop(True)
    return data


AGENTS = sorted((GH / "agents").glob("*.agent.md"))
WORKFLOWS = sorted((GH / "workflows").glob("*.yml"))


def test_four_specialists_exist():
    assert {p.name.removesuffix(".agent.md") for p in AGENTS} == {
        "devassist-tests", "devassist-docs", "devassist-git", "devassist-trace"}


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.name)
def test_custom_agent_format(path):
    meta, body = split_frontmatter(path)
    assert set(meta) <= AGENT_KEYS, f"unknown keys: {set(meta) - AGENT_KEYS}"
    assert isinstance(meta["description"], str) and meta["description"].strip()
    assert meta.get("target", "github-copilot") in {"vscode", "github-copilot"}
    for tool in meta.get("tools", []):
        assert tool.lower() in TOOL_ALIASES or re.fullmatch(r"[\w.-]+/(\*|[\w.-]+)", tool), tool
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in meta.get("metadata", {}).items())
    assert len(body) <= 30_000


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.name)
def test_custom_agents_keep_guardrails(path):
    _, body = split_frontmatter(path)
    text = body.lower()
    assert "never merge" in text or "cannot do" in text
    assert "copilot/*" in text
    assert "main" in text


def test_git_agent_cannot_edit_files():
    meta, _ = split_frontmatter(GH / "agents" / "devassist-git.agent.md")
    assert not {"edit", "execute"} & {t.lower() for t in meta["tools"]}


def test_repository_instructions_exist_and_state_rules():
    text = (GH / "copilot-instructions.md").read_text(encoding="utf-8")
    assert "python -m pytest" in text
    assert "Never merge" in text and "copilot/*" in text and "ai/*" in text


@pytest.mark.parametrize("path", sorted((GH / "instructions").glob("*")), ids=lambda p: p.name)
def test_path_instructions_format(path):
    assert path.name.endswith(".instructions.md")
    meta, body = split_frontmatter(path)
    assert set(meta) <= {"applyTo", "excludeAgent"}
    assert isinstance(meta["applyTo"], str) and meta["applyTo"].strip()
    assert meta.get("excludeAgent", "code-review") in {"code-review", "cloud-agent"}
    assert body.strip()


def test_path_instructions_match_real_files():
    for path in (GH / "instructions").glob("*.instructions.md"):
        meta, _ = split_frontmatter(path)
        patterns = [p.strip() for p in meta["applyTo"].split(",")]
        assert any(list(ROOT.glob(p)) for p in patterns), f"{path.name}: applyTo matches nothing"


def test_copilot_setup_steps_follows_the_docs():
    wf = load_workflow(GH / "workflows" / "copilot-setup-steps.yml")
    assert list(wf["jobs"]) == ["copilot-setup-steps"]
    job = wf["jobs"]["copilot-setup-steps"]
    assert set(job) <= SETUP_JOB_KEYS
    assert job["timeout-minutes"] <= 59
    assert job["permissions"] == {"contents": "read"}
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert 'pip install -e ".[dev]"' in runs and "python -m pytest" in runs
    assert "workflow_dispatch" in wf["on"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflows_need_no_secrets_and_least_privilege(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\$\{\{[^}]*secrets\.", text)
    assert "pull_request_target" not in text
    wf = load_workflow(path)
    assert wf["jobs"]
    perms = wf.get("permissions") or {}
    for job in wf["jobs"].values():
        perms = {**perms, **(job.get("permissions") or {})}
        assert job["runs-on"] == "ubuntu-latest"  # standard runner: free on public repositories
    assert perms, "set explicit permissions"
    assert "contents" in perms and perms["contents"] == "read"


def test_ci_runs_pytest():
    wf = load_workflow(GH / "workflows" / "ci.yml")
    runs = [s.get("run", "") for s in wf["jobs"]["pytest"]["steps"]]
    assert "python -m pytest -q" in runs


def test_paid_trace_workflow_is_off_by_default():
    wf = load_workflow(GH / "workflows" / "copilot-trace-on-failure.yml")
    condition = wf["jobs"]["diagnose"]["if"]
    assert "vars.COPILOT_TRACE_ENABLED == 'true'" in condition
    assert "head_repository.full_name == github.repository" in condition
    steps = wf["jobs"]["diagnose"]["steps"]
    mask_index = next(i for i, s in enumerate(steps) if "devassist.masking" in s.get("run", ""))
    copilot_index = next(i for i, s in enumerate(steps) if "copilot -p" in s.get("run", ""))
    assert mask_index < copilot_index
    assert "--allow-tool" not in steps[copilot_index]["run"]


def test_ruleset_uses_documented_fields():
    ruleset = json.loads((ROOT / "docs" / "github-copilot" / "ruleset-main.json").read_text(encoding="utf-8"))
    assert ruleset["target"] == "branch" and ruleset["enforcement"] == "active"
    assert ruleset["bypass_actors"] == []
    assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    rules = {r["type"]: r.get("parameters", {}) for r in ruleset["rules"]}
    assert set(rules) <= RULE_TYPES
    pr = rules["pull_request"]
    for required in ("dismiss_stale_reviews_on_push", "require_code_owner_review", "require_last_push_approval",
                     "required_approving_review_count", "required_review_thread_resolution"):
        assert required in pr
    assert 1 <= pr["required_approving_review_count"] <= 10
    checks = rules["required_status_checks"]
    ci = load_workflow(GH / "workflows" / "ci.yml")
    assert {c["context"] for c in checks["required_status_checks"]} <= set(ci["jobs"])
    assert {"deletion", "non_fast_forward"} <= set(rules)


def _load_doc_check():
    spec = importlib.util.spec_from_file_location("check_doc_only_changes",
                                                  ROOT / "scripts" / "check_doc_only_changes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doc_only_check_accepts_docstrings_and_comments():
    check = _load_doc_check()
    before = "def add(a, b):\n    return a + b\n"
    after = 'def add(a, b):\n    """Add two numbers."""\n    # simple\n    return a + b\n'
    assert check.only_docs_changed(before, after)


def test_doc_only_check_rejects_code_changes():
    check = _load_doc_check()
    before = "def add(a, b):\n    return a + b\n"
    after = 'def add(a, b):\n    """Add two numbers."""\n    return a - b\n'
    assert not check.only_docs_changed(before, after)
