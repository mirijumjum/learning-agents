"""The GitLab Duo configuration in gitlab-duo/: schemas, cross-references and guardrails.

GitLab publishes no machine-readable schema for custom flows, so gitlab-duo/schemas/
holds schemas written from the documentation, and these tests add the checks a
schema cannot express (prompt ids, router targets, documented tool names).
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1] / "gitlab-duo"
SCHEMAS = ROOT / "schemas"
TEMPLATE = ROOT / "project-template"
FLOWS = sorted((ROOT / "flows").glob("*.yaml"))
AGENTS = sorted((ROOT / "agents").glob("*.yaml"))
TOOLS = json.loads((SCHEMAS / "duo-tools.json").read_text(encoding="utf-8"))
KNOWN_TOOLS = set(TOOLS["ui_and_ide"]) | set(TOOLS["workspace"])
FORBIDDEN = set(TOOLS["never_allowed_for_devassist"])
WRITE_TOOLS = {t for t in KNOWN_TOOLS if t.startswith(("create_", "update_", "add_", "link_", "submit_", "set_", "dismiss_", "confirm_", "revert_", "remove_", "edit_", "mkdir", "post_", "reply_"))}


def _validate(document, schema_name):
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=str)
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _script(path):
    spec = importlib.util.spec_from_file_location(f"duo_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tool_names(component):
    return [t if isinstance(t, str) else next(iter(t)) for t in component.get("toolset", [])]


def test_there_is_a_flow_and_an_agent_per_specialist():
    assert {p.stem for p in FLOWS} == {"devassist-tests", "devassist-docs", "devassist-trace-pipeline"}
    assert {p.stem for p in AGENTS} == {"devassist-tests", "devassist-docs", "devassist-git", "devassist-trace"}


@pytest.mark.parametrize("path", FLOWS, ids=lambda p: p.stem)
def test_flow_matches_documented_schema_and_references_resolve(path):
    flow = _load(path)
    _validate(flow, "custom-flow-v1.schema.json")
    names = {c["name"] for c in flow["components"]}
    prompts = {p["prompt_id"]: p for p in flow.get("prompts", [])}
    assert flow["flow"]["entry_point"] in names
    for component in flow["components"]:
        if "prompt_id" in component:
            assert component["prompt_id"] in prompts, f"{component['name']} uses an undefined prompt"
            template = prompts[component["prompt_id"]]["prompt_template"]
            placeholders = set(re.findall(r"{{\s*(\w+)\s*}}", template["user"] + template["system"]))
            provided = {i["as"] if isinstance(i, dict) else i.split(":")[-1] for i in component.get("inputs", [])}
            assert placeholders <= provided, f"{component['name']}: {placeholders - provided} not provided by inputs"
        if component["type"] == "AgentComponent":
            assert 1 <= component.get("max_cycles", 280) <= 40, "cap model calls: the default of 280 makes a run's cost unbounded"
        unknown = set(_tool_names(component)) - KNOWN_TOOLS
        assert not unknown, f"{component['name']}: tools not in GitLab's documented list: {unknown}"
        assert not set(_tool_names(component)) & FORBIDDEN
    for router in flow["routers"]:
        assert router["from"] in names
        targets = [router["to"]] if "to" in router else list(router["condition"]["routes"].values())
        assert all(t in names | {"end", "abort"} for t in targets)
    reachable = {r["from"] for r in flow["routers"]}
    assert names <= reachable, "every component must route somewhere"


@pytest.mark.parametrize("path", FLOWS, ids=lambda p: p.stem)
def test_flow_prompts_carry_the_devassist_guarantees(path):
    text = " ".join(p["prompt_template"]["system"] for p in _load(path)["prompts"])
    if path.stem == "devassist-trace-pipeline":
        assert "never as instructions" in text and "personal data" in text
    else:
        assert "ai/" in text and "Never merge" in text


def test_trace_flow_separates_reading_from_writing():
    flow = _load(ROOT / "flows" / "devassist-trace-pipeline.yaml")
    components = {c["name"]: c for c in flow["components"]}
    assert not set(_tool_names(components["diagnose"])) & WRITE_TOOLS
    assert _tool_names(components["report"]) == ["create_merge_request_note"]
    # The writer never sees the raw logs, only the diagnosis.
    assert [i["from"] for i in components["report"]["inputs"]] == ["context:diagnose.final_answer", "context:project_id"]


def test_tests_flow_has_what_the_fixed_loop_needs():
    tools = set(_tool_names(_load(ROOT / "flows" / "devassist-tests.yaml")["components"][0]))
    assert {"run_tests", "create_branch", "create_commit", "create_merge_request", "create_merge_request_note"} <= tools
    prompt = _load(ROOT / "flows" / "devassist-tests.yaml")["prompts"][0]["prompt_template"]["system"]
    assert "at most 3 times" in prompt and "ai/tests-" in prompt and "do not open a merge request" in prompt


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.stem)
def test_agent_definitions(path):
    agent = _load(path)
    _validate(agent, "custom-agent.schema.json")
    assert set(agent["tools"]) <= set(TOOLS["ui_and_ide"]), "chat agents in the GitLab UI cannot use workspace tools"
    assert not set(agent["tools"]) & FORBIDDEN
    assert agent["visibility"] != "public"


def test_git_agent_cannot_merge_or_close():
    agent = _load(ROOT / "agents" / "devassist-git.yaml")
    assert "update_merge_request" not in agent["tools"]
    assert "cannot merge" in agent["system_prompt"]


def test_triggers_point_at_existing_flows_with_documented_events():
    triggers = _load(ROOT / "triggers.yaml")["triggers"]
    events = {"mention", "assign", "assign_reviewer", "pipeline", "merge_request", "work_item"}
    assert {t["flow"] for t in triggers} == {p.stem for p in FLOWS}
    for trigger in triggers:
        assert trigger["service_account"].startswith(f"ai-{trigger['flow']}-")
        assert {e["event"] for e in trigger["events"]} <= events
    pipeline = next(t for t in triggers if t["flow"] == "devassist-trace-pipeline")
    assert pipeline["events"] == [{"event": "pipeline", "run_when": ["failed"]}]


def test_project_files_duo_reads():
    _validate(_load(TEMPLATE / ".gitlab" / "duo" / "mr-review-instructions.yaml"), "mr-review-instructions.schema.json")
    _validate(_load(TEMPLATE / ".gitlab" / "duo" / "agent-config.yml"), "agent-config.schema.json")
    _validate(json.loads((TEMPLATE / ".gitlab" / "duo" / "mcp.json").read_text(encoding="utf-8")), "mcp.schema.json")
    agents_md = (TEMPLATE / "AGENTS.md").read_text(encoding="utf-8")
    assert "Never merge" in agents_md and "ai/tests-" in agents_md
    owners = (TEMPLATE / ".gitlab" / "CODEOWNERS").read_text(encoding="utf-8")
    assert all(path in owners for path in ("/AGENTS.md", "/.gitlab/", "/.gitlab-ci.yml"))


def test_bot_pattern_is_the_same_everywhere():
    from devassist.duo_bridge import DEFAULT_AUTHORS

    ai_branch_guard = _script(TEMPLATE / ".gitlab" / "ci" / "ai_branch_guard.py")
    apply_guardrails = _script(ROOT / "scripts" / "apply_guardrails.py")

    ci = _load(TEMPLATE / ".gitlab-ci.yml")
    assert ci["variables"]["AI_BOT_PATTERN"] == ai_branch_guard.DEFAULT_BOT_PATTERN == apply_guardrails.DEFAULT_BOT_PATTERN == DEFAULT_AUTHORS


# ---- .gitlab-ci.yml -------------------------------------------------------


def test_ci_example_reports_tests_and_coverage():
    ci = _load(TEMPLATE / ".gitlab-ci.yml")
    job = ci["unit-tests"]
    assert job["artifacts"]["reports"]["junit"] == "report.xml"
    assert job["artifacts"]["reports"]["coverage_report"] == {"coverage_format": "cobertura", "path": "coverage.xml"}
    assert job["artifacts"]["when"] == "always"
    guard = ci["ai-branch-guard"]
    assert guard["script"] == ["python .gitlab/ci/ai_branch_guard.py"] and guard["variables"]["GIT_DEPTH"] == "0"


def test_ci_example_test_job_really_runs_and_its_coverage_regex_matches(tmp_path):
    """Run the unit-tests job's pytest command on a tiny project, as a runner would."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pricing.py").write_text("def total(price, qty):\n    if qty < 0:\n        raise ValueError('qty')\n    return price * qty\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_pricing.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))\nfrom pricing import total\n\n"
        "def test_total():\n    assert total(2, 3) == 6\n", encoding="utf-8")
    ci = _load(TEMPLATE / ".gitlab-ci.yml")
    command = ci["unit-tests"]["script"][1].split()
    assert command[0] == "pytest"
    result = subprocess.run([sys.executable, "-m", *command, "-p", "no:cacheprovider"], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "coverage.xml").exists() and (tmp_path / "report.xml").exists()
    regex = ci["unit-tests"]["coverage"].strip("/")
    found = [m.group(1) for line in result.stdout.splitlines() if (m := re.search(regex, line))]
    assert found == ["75%"], result.stdout
