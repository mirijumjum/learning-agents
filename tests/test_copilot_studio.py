"""The Copilot Studio alternative: backend tools, MCP server, REST connector, guards and agent files.

The model that Copilot Studio would provide is played by fixed test files (the same
ones the offline mock model writes); everything else is real: the MCP and REST
endpoints, the GitLab stand-in and its CI, which runs pytest.
"""

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from openapi_spec_validator import validate

from devassist.config import Settings
from devassist.dev_gitlab import FakeGitLab, seed_sample_logs
from devassist.mock_llm import _GOOD_TESTS, _WRONG_TEST
from devassist.specialists import _FILE_BLOCK
from devassist_copilot import costing
from devassist_copilot.cli import write_connector_files
from devassist_copilot.mcp import SUPPORTED_VERSIONS
from devassist_copilot.openapi import build_mcp_spec, build_rest_spec
from devassist_copilot.server import AuthConfig, build_backend, check_startup, create_app
from devassist_copilot.sessions import SessionStore
from devassist_copilot.tools import TOOLS, TOOLS_BY_NAME

ROOT = Path(__file__).resolve().parents[1]
CS = ROOT / "copilot-studio"
PAYMENTS = "acme/payments-api"
API_KEY = "k" * 32


def _file(answer: str) -> tuple[str, str]:
    match = _FILE_BLOCK.search(answer)
    return match.group(1), match.group(2)


class Harness:
    """The GitLab stand-in plus the backend served over HTTP, as Copilot Studio would reach it."""

    def __init__(self, tmp_path, auth=AuthConfig(), **overrides):
        self.tmp_path, self.auth, self.overrides = tmp_path, auth, overrides

    async def __aenter__(self):
        logs = self.tmp_path / "logs.jsonl"
        seed_sample_logs(logs)
        self.fake = FakeGitLab()
        self.gitlab_runner = web.AppRunner(self.fake.app())
        await self.gitlab_runner.setup()
        site = web.TCPSite(self.gitlab_runner, "127.0.0.1", 0)
        await site.start()
        self.fake.base_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        settings = Settings(profile="mock", gitlab_url=self.fake.base_url, gitlab_token="t", gitlab_projects=(PAYMENTS, "acme/customer-portal"),
                            log_jsonl_path=logs, audit_path=self.tmp_path / "audit.jsonl", pipeline_poll_seconds=0.1, **self.overrides)
        self.backend = build_backend(settings)
        self.client = TestClient(TestServer(create_app(self.backend, self.auth)))
        await self.client.start_server()
        self.headers = {"X-API-Key": API_KEY} if self.auth.api_key else {}
        self._id = 0
        return self

    async def __aexit__(self, *_):
        await self.client.close()
        await self.gitlab_runner.cleanup()

    async def rest(self, tool, headers=None, **arguments):
        response = await self.client.post(f"/api/tools/{tool}", json=arguments, headers={**self.headers, **(headers or {})})
        return response.status, await response.json()

    async def ok(self, tool, **arguments):
        status, body = await self.rest(tool, **arguments)
        assert status == 200, body
        return body

    async def mcp(self, method, params=None, headers=None):
        self._id += 1
        response = await self.client.post("/mcp", json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
                                          headers={**self.headers, **(headers or {})})
        assert response.status == 200, await response.text()
        return await response.json()

    async def wait(self, session_id):
        """What the WaitForTestRun agent flow does: poll until CI is no longer running."""
        for _ in range(200):
            run = await self.ok("GetTestRun", sessionId=session_id, waitSeconds=2)
            if run["status"] != "running":
                return run
        raise AssertionError("CI never finished")

    def ai_mrs(self, project=PAYMENTS):
        return [mr for mr in self.fake.projects[project]["mrs"].values() if mr["source_branch"].startswith("ai/")]


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ test loop
def test_full_test_loop_runs_ci_and_opens_an_mr(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            started = await h.ok("StartTestSession", project="payments-api", mergeRequestIid=231, symbol="OrderService")
            assert started["branch"].startswith("ai/tests-") and started["status"] == "ready"
            assert any(f["path"] == "app/order_service.py" for f in started["sourceFiles"]) and "apply_discount" in started["diff"]
            assert started["styleExample"]["path"] == "tests/test_smoke.py"

            path, content = _file(_GOOD_TESTS)
            submitted = await h.ok("SubmitTestFile", sessionId=started["sessionId"], path=path, content=content)
            assert submitted["status"] == "running" and submitted["attempt"] == 1

            result = await h.wait(started["sessionId"])
            assert result["status"] == "passed" and result["passing"] == 6 and result["failing"] == 0
            opened = await h.ok("OpenTestMergeRequest", sessionId=started["sessionId"])
            assert opened["summary"] == "4 tests added. Pipeline: 6 passing, 0 failing. Coverage 38% → 100%."
            mrs = h.ai_mrs()
            assert len(mrs) == 1 and mrs[0]["state"] == "opened" and mrs[0]["target_branch"] == "feature/order-discount"
            assert opened["mergeRequestUrl"] == mrs[0]["web_url"]
            # the source branch is untouched; the tests live only on the ai/ branch
            assert "tests/test_order_service.py" not in h.fake.projects[PAYMENTS]["branches"]["feature/order-discount"]
            # asking again does not open a second MR
            assert (await h.ok("OpenTestMergeRequest", sessionId=started["sessionId"]))["mergeRequestUrl"] == opened["mergeRequestUrl"]
            assert len(h.ai_mrs()) == 1

    run(go())


def test_wrong_test_is_reported_by_ci_and_fixed(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            started = await h.ok("StartTestSession", project="payments-api", mergeRequestIid=231)
            path, wrong = _file(_WRONG_TEST)
            await h.ok("SubmitTestFile", sessionId=started["sessionId"], path=path, content=wrong)
            failed = await h.wait(started["sessionId"])
            assert failed["status"] == "failed" and failed["attemptsLeft"] == 2 and "assert" in failed["failedLog"]
            status, refused = await h.rest("OpenTestMergeRequest", sessionId=started["sessionId"])
            assert status == 400 and "only opened after CI passes" in refused["error"]

            await h.ok("SubmitTestFile", sessionId=started["sessionId"], path=path, content=_file(_GOOD_TESTS)[1])
            assert (await h.wait(started["sessionId"]))["status"] == "passed"
            ran = sorted((p for p in h.fake.pipelines.values() if p["ref"] == started["branch"]), key=lambda p: p["id"])
            assert [p["status"] for p in ran] == ["failed", "success"]

    run(go())


def test_real_bug_stops_the_loop_without_an_mr(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            started = await h.ok("StartTestSession", project="payments-api", mergeRequestIid=232)
            path, content = _file(_GOOD_TESTS)
            await h.ok("SubmitTestFile", sessionId=started["sessionId"], path=path, content=content)
            assert (await h.wait(started["sessionId"]))["status"] == "failed"
            stopped = await h.ok("ReportCodeBug", sessionId=started["sessionId"], explanation="apply_discount divides by 10, not 100.")
            assert stopped["status"] == "bug_reported" and "divides by 10" in stopped["summary"]
            assert h.ai_mrs() == []
            status, _ = await h.rest("SubmitTestFile", sessionId=started["sessionId"], path=path, content=content)
            assert status == 400

    run(go())


def test_bug_cannot_be_reported_before_ci_shows_it(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            started = await h.ok("StartTestSession", project="payments-api", mergeRequestIid=231)
            status, body = await h.rest("ReportCodeBug", sessionId=started["sessionId"], explanation="made up")
            assert status == 400 and "red pipeline" in body["error"]

    run(go())


def test_attempts_are_limited_and_exhaustion_opens_no_mr(tmp_path):
    async def go():
        async with Harness(tmp_path, max_test_attempts=2) as h:
            started = await h.ok("StartTestSession", project="payments-api", mergeRequestIid=231)
            path, wrong = _file(_WRONG_TEST)
            for _ in range(2):
                await h.ok("SubmitTestFile", sessionId=started["sessionId"], path=path, content=wrong)
                last = await h.wait(started["sessionId"])
            assert last["status"] == "exhausted" and "no merge request" in last["summary"]
            status, body = await h.rest("SubmitTestFile", sessionId=started["sessionId"], path=path, content=wrong)
            assert status == 400 and "exhausted" in body["error"]
            assert h.ai_mrs() == []

    run(go())


@pytest.mark.parametrize("path", ["app/order_service.py", ".gitlab-ci.yml", "tests/../app/x.py", "/tests/test_x.py", "tests/notes.md"])
def test_only_test_files_can_be_committed(tmp_path, path):
    async def go():
        async with Harness(tmp_path) as h:
            started = await h.ok("StartTestSession", project="payments-api", mergeRequestIid=231)
            status, body = await h.rest("SubmitTestFile", sessionId=started["sessionId"], path=path, content="x = 1\n")
            assert status == 400 and "nothing was committed" in body["error"]
            assert h.fake.projects[PAYMENTS]["branches"][started["branch"]] == h.fake.projects[PAYMENTS]["branches"]["feature/order-discount"]

    run(go())


def test_session_edits_one_file_and_never_overwrites_a_human_test(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            started = await h.ok("StartTestSession", project="payments-api", mergeRequestIid=231)
            # tests/test_smoke.py was written by a person: the agent's file gets another name
            first = await h.ok("SubmitTestFile", sessionId=started["sessionId"], path="tests/test_smoke.py", content=_file(_WRONG_TEST)[1])
            assert first["testPath"] == "tests/test_smoke_ai.py"
            await h.wait(started["sessionId"])
            status, body = await h.rest("SubmitTestFile", sessionId=started["sessionId"], path="tests/test_other.py", content="x = 1\n")
            assert status == 400 and "only edits tests/test_smoke_ai.py" in body["error"]
            branch = h.fake.projects[PAYMENTS]["branches"][started["branch"]]
            assert branch["tests/test_smoke.py"] == h.fake.projects[PAYMENTS]["branches"]["develop"]["tests/test_smoke.py"]

    run(go())


def test_sessions_belong_to_their_creator(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            started = await h.ok("StartTestSession", headers={"X-DevAssist-Requester": "ana@acme.test"}, project="payments-api", mergeRequestIid=231)
            status, body = await h.rest("SubmitTestFile", headers={"X-DevAssist-Requester": "eve@acme.test"},
                                        sessionId=started["sessionId"], path="tests/test_a.py", content="x = 1\n")
            assert status == 400 and "someone else" in body["error"]

    run(go())


def test_large_diffs_are_refused_before_any_work(tmp_path):
    async def go():
        async with Harness(tmp_path, max_diff_chars=50) as h:
            status, body = await h.rest("StartTestSession", project="payments-api", mergeRequestIid=231)
            assert status == 400 and "limit" in body["error"]
            assert not [b for b in h.fake.projects[PAYMENTS]["branches"] if b.startswith("ai/")]

    run(go())


def test_sessions_survive_a_restart(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    session = store.create(project=PAYMENTS, source_ref="develop", branch="ai/tests-x", label="x", created_by="", max_attempts=3)
    session.status = "running"
    store.save(session)
    assert SessionStore(tmp_path / "s.json").get(session.id).status == "running"


# ------------------------------------------------------------------ guards on Git, docs and traces
def test_projects_outside_the_allow_list_are_refused(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            status, body = await h.rest("ListMergeRequests", project="acme/secret-service")
            assert status == 400 and "not enabled" in body["error"]
            assert (await h.ok("ListProjects"))["projects"] == [PAYMENTS, "acme/customer-portal"]

    run(go())


def test_no_tool_can_merge_deploy_or_delete():
    names = " ".join(TOOLS_BY_NAME).lower()
    for word in ("merge(", "deploy", "delete", "remove", "approve", "push"):
        assert word not in names
    assert {t.name for t in TOOLS if not t.read_only} <= {
        "CreateBranch", "CommentOnMergeRequest", "CreateIssue", "StartTestSession", "SubmitTestFile", "OpenTestMergeRequest",
        "ReportCodeBug", "OpenApiReferenceMergeRequest", "OpenCodeCommentsMergeRequest", "OpenChangelogMergeRequest", "RecordFeedback"}


def test_git_tools_and_assigned_to_me(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            mine = await h.ok("ListMergeRequests", headers={"X-DevAssist-Requester": "dev@acme.test"}, project="payments-api", assignedToMe=True)
            assert [mr["iid"] for mr in mine["mergeRequests"]] == [231]
            status, body = await h.rest("ListMergeRequests", project="payments-api", assignedToMe=True)
            assert status == 400 and "don't know who you are" in body["error"]
            branch = await h.ok("CreateBranch", project="payments-api", branch="feature/invoice-pdf", ref="develop")
            assert "feature/invoice-pdf" in h.fake.projects[PAYMENTS]["branches"] and branch["url"].startswith(h.fake.base_url)
            status, _ = await h.rest("CreateBranch", project="payments-api", branch="bad name", ref="develop")
            assert status == 400
            pipeline = await h.ok("GetPipelineStatus", project="payments-api", pipelineId="#5512")
            assert pipeline["status"] == "failed"

    run(go())


def test_text_posted_to_gitlab_is_masked(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.ok("CreateIssue", headers={"X-DevAssist-Requester": "dev@acme.test"}, project="payments-api",
                       title="Checkout fails for jane@example.com", description="Seen from 10.0.4.7, customer_id=C9001")
            issue = h.fake.projects[PAYMENTS]["issues"][0]
            text = issue["title"] + issue["description"]
            assert "jane@example.com" not in text and "10.0.4.7" not in text and "C9001" not in text
            assert "on behalf of dev@acme.test" in issue["description"]
            await h.ok("CommentOnMergeRequest", project="payments-api", mergeRequestIid=231, body="ping bob@example.com")
            assert "bob@example.com" not in h.fake.projects[PAYMENTS]["notes"][0]["body"]

    run(go())


def test_incident_evidence_is_counted_in_code_and_masked(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            evidence = await h.ok("InvestigateIncident", question="Why did checkout fail yesterday between 17:00 and 18:00?", project="payments-api")
            assert evidence["service"] == "checkout" and evidence["totalErrors"] == 49 and evidence["groupCount"] == 2
            assert evidence["groups"][0]["count"] == 42
            dumped = json.dumps(evidence)
            assert "@example.com" not in dumped and "10.0.4." not in dumped and "<email>" in dumped
            assert len(evidence["deployments"]) == 1

            red = await h.ok("GetFailedPipelineLog", project="payments-api", pipelineId=5512)
            assert red["job"]["name"] == "unit-tests" and "ModuleNotFoundError" in red["log"]

    run(go())


def test_docs_tools_open_mrs_and_reject_code_changes(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            sources = await h.ok("GetDocsSources", project="customer-portal", kind="api")
            assert [f["path"] for f in sources["files"]] == ["controllers/customers.py"]
            api = await h.ok("OpenApiReferenceMergeRequest", project="customer-portal", markdown="```markdown\n# API\n\n## GET /customers\n```")
            mr = h.ai_mrs("acme/customer-portal")[0]
            branch = h.fake.projects["acme/customer-portal"]["branches"][mr["source_branch"]]
            assert branch["docs/api.md"] == "# API\n\n## GET /customers\n" and "docs/api.md" in branch["README.md"]
            assert api["mergeRequestUrl"] == mr["web_url"]

            original = sources["files"][0]["content"]
            documented = original.replace("def list_customers():\n", 'def list_customers():\n    """Return all customers."""\n')
            sneaky = original.replace("return list(_CUSTOMERS.values())", "return []")
            result = await h.ok("OpenCodeCommentsMergeRequest", project="customer-portal", files=[
                {"path": "controllers/customers.py", "content": documented}, {"path": "portal/__init__.py", "content": sneaky}])
            assert result["changed"] == ["controllers/customers.py"] and result["rejected"] == ["portal/__init__.py"]
            status, body = await h.rest("OpenCodeCommentsMergeRequest", project="customer-portal",
                                        files=[{"path": "controllers/customers.py", "content": sneaky}])
            assert status == 400 and "without changing code" in body["error"]

            changelog = await h.ok("OpenChangelogMergeRequest", project="payments-api")
            assert "2 merged MR" in changelog["summary"]

    run(go())


# ------------------------------------------------------------------ MCP server
def test_mcp_handshake_list_and_call(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            init = await h.mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
            assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
            old = await h.mcp("initialize", {"protocolVersion": "1999-01-01"})
            assert old["result"]["protocolVersion"] == SUPPORTED_VERSIONS[0]
            note = await h.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            assert note.status == 202

            listed = (await h.mcp("tools/list"))["result"]["tools"]
            assert [t["name"] for t in listed] == [t.name for t in TOOLS]
            assert all(t["inputSchema"]["type"] == "object" and t["description"] for t in listed)
            assert next(t for t in listed if t["name"] == "GetTestRun")["annotations"]["readOnlyHint"] is True

            called = (await h.mcp("tools/call", {"name": "ListProjects", "arguments": {}}))["result"]
            assert called["isError"] is False and called["structuredContent"]["projects"][0] == PAYMENTS
            assert json.loads(called["content"][0]["text"]) == called["structuredContent"]

            refused = (await h.mcp("tools/call", {"name": "ListMergeRequests", "arguments": {"project": "acme/secret"}}))["result"]
            assert refused["isError"] is True and "not enabled" in refused["content"][0]["text"]
            assert (await h.mcp("tools/call", {"name": "Nope"}))["error"]["code"] == -32602
            assert (await h.mcp("resources/list"))["error"]["code"] == -32601
            assert (await h.mcp("ping"))["result"] == {}

            bad = await h.client.post("/mcp", data=b"{not json")
            assert (await bad.json())["error"]["code"] == -32700
            assert (await h.client.get("/mcp")).status == 405

    run(go())


def test_mcp_runs_the_test_loop_too(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            async def call(name, **arguments):
                result = (await h.mcp("tools/call", {"name": name, "arguments": arguments}))["result"]
                assert not result["isError"], result
                return result["structuredContent"]

            started = await call("StartTestSession", project="payments-api", mergeRequestIid="!231")
            path, content = _file(_GOOD_TESTS)
            await call("SubmitTestFile", sessionId=started["sessionId"], path=path, content=content)
            for _ in range(200):
                run_ = await call("GetTestRun", sessionId=started["sessionId"], waitSeconds=2)
                if run_["status"] != "running":
                    break
            assert run_["status"] == "passed"
            assert (await call("OpenTestMergeRequest", sessionId=started["sessionId"]))["summary"].startswith("4 tests added.")

    run(go())


def test_mcp_rejects_foreign_browser_origins(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            response = await h.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "https://evil.example"})
            assert response.status == 403
            assert (await h.mcp("ping", headers={"Origin": "http://localhost:6274"}))["result"] == {}

    run(go())


# ------------------------------------------------------------------ authentication
def test_api_key_is_required(tmp_path):
    async def go():
        async with Harness(tmp_path, auth=AuthConfig("apikey", API_KEY)) as h:
            assert (await h.client.post("/api/tools/ListProjects", json={})).status == 401
            assert (await h.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"X-API-Key": "wrong" * 8})).status == 401
            assert (await h.ok("ListProjects"))["projects"]
            assert (await h.client.get("/healthz")).status == 200

    run(go())


def test_easy_auth_identity_is_the_caller(tmp_path):
    async def go():
        async with Harness(tmp_path, auth=AuthConfig("easyauth")) as h:
            assert (await h.client.post("/api/tools/ListProjects", json={})).status == 401
            mine = await h.ok("ListMergeRequests", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "Dev@acme.test", "X-DevAssist-Requester": "sam@acme.test"},
                              project="payments-api", assignedToMe=True)
            assert [mr["iid"] for mr in mine["mergeRequests"]] == [231]  # the platform's identity wins over the header
            audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
            assert any(r["kind"] == "tool_call" and r.get("user") == "dev@acme.test" for r in audit)

    run(go())


def test_unsafe_startup_configurations_are_refused():
    with pytest.raises(RuntimeError, match="without authentication"):
        check_startup(AuthConfig(), "0.0.0.0", Settings(gitlab_projects=(PAYMENTS,)))
    with pytest.raises(RuntimeError, match="GITLAB_PROJECTS"):
        check_startup(AuthConfig("apikey", API_KEY), "0.0.0.0", Settings())
    with pytest.raises(RuntimeError, match="24 characters"):
        AuthConfig.from_env({"DEVASSIST_COPILOT_AUTH": "apikey", "DEVASSIST_COPILOT_API_KEY": "short"})
    assert AuthConfig.from_env({"DEVASSIST_COPILOT_API_KEY": API_KEY}).mode == "apikey"
    check_startup(AuthConfig("easyauth"), "0.0.0.0", Settings(gitlab_projects=(PAYMENTS,)))


# ------------------------------------------------------------------ connector and agent files
def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_connector_specs_are_valid_swagger_2_and_committed_up_to_date(tmp_path):
    for spec in (build_rest_spec(), build_mcp_spec()):
        validate(spec)
        assert spec["swagger"] == "2.0"
        for node in _walk(spec):
            assert not {"oneOf", "anyOf", "allOf", "$ref"} & set(node), node
    rest = build_rest_spec()
    operations = [op["post"] for op in rest["paths"].values()]
    assert [op["operationId"] for op in operations] == [t.name for t in TOOLS]
    assert all(len(op["summary"]) <= 80 and op["description"] for op in operations)
    assert build_mcp_spec()["paths"]["/mcp"]["post"]["x-ms-agentic-protocol"] == "mcp-streamable-1.0"

    write_connector_files(tmp_path, "devassist.example.com")
    for generated in tmp_path.rglob("*.json"):
        committed = CS / "connector" / generated.relative_to(tmp_path)
        assert committed.read_text(encoding="utf-8") == generated.read_text(encoding="utf-8"), f"run devassist-copilot export-connector ({committed})"


def test_agent_definition_matches_instructions_and_tools():
    instructions = (CS / "agent" / "instructions.md").read_text(encoding="utf-8").rstrip("\n")
    agent = yaml.safe_load((CS / "agent" / "agent.mcs.yml").read_text(encoding="utf-8"))
    assert agent["kind"] == "GptComponentMetadata" and agent["instructions"] == instructions
    assert len(instructions) <= 8000  # Copilot Studio's limit for agent instructions
    for name in ("StartTestSession", "SubmitTestFile", "GetTestRun", "OpenTestMergeRequest", "ReportCodeBug", "InvestigateIncident",
                 "GetFailedPipelineLog", "OpenApiReferenceMergeRequest", "OpenChangelogMergeRequest", "CreateIssue", "WaitForTestRun"):
        assert name in instructions
    mentioned = {word.strip(".,:()") for word in instructions.split() if word[:1].isupper() and word.strip(".,:()") in TOOLS_BY_NAME}
    assert mentioned <= set(TOOLS_BY_NAME)
    for path in (CS / "agent").rglob("*.y*ml"):
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None, path
    mcp_tool = yaml.safe_load((CS / "agent" / "actions" / "DevAssistMCP.mcs.yml").read_text(encoding="utf-8"))
    assert mcp_tool["action"]["operationDetails"]["operationId"] == build_mcp_spec()["paths"]["/mcp"]["post"]["operationId"]
    references = yaml.safe_load((CS / "agent" / "connectionreferences.mcs.yml").read_text(encoding="utf-8"))["connectionReferences"]
    assert mcp_tool["action"]["connectionReference"] in {r["connectionReferenceLogicalName"] for r in references}


def test_agent_flow_polls_a_real_operation_with_a_timeout():
    flow = json.loads((CS / "agent" / "workflows" / "WaitForTestRun" / "workflow.json").read_text(encoding="utf-8"))
    definition = flow["properties"]["definition"]
    assert definition["triggers"]["manual"]["kind"] == "Skills"  # "When an agent calls the flow"
    loop = definition["actions"]["Wait_for_CI"]
    assert loop["type"] == "Until" and loop["limit"]["timeout"].startswith("PT")
    rest = build_rest_spec()
    operations = {op["post"]["operationId"]: op["post"] for op in rest["paths"].values()}
    for action in _walk(definition):
        if action.get("type") == "OpenApiConnection":
            op = operations[action["inputs"]["host"]["operationId"]]
            body = op["parameters"][0]["schema"]["properties"]
            for parameter in action["inputs"]["parameters"]:
                assert parameter.removeprefix("body/") in body
            assert TOOLS_BY_NAME[action["inputs"]["host"]["operationId"]].read_only  # polling never writes
    assert definition["actions"]["Respond_to_the_agent"]["kind"] == "Skills"


def test_cost_estimate_is_pinned():
    pilot, team = costing.estimate(5), costing.estimate(20)
    assert (pilot.credits, pilot.best_usd, pilot.packs) == (7_022, 70.22, 0)
    assert (team.credits, team.payg_usd, team.best_usd, team.packs) == (28_090, 280.9, 230.9, 1)
    assert team.licensed_usd == 110.04
    assert costing.reasoning_surcharge_usd(62) == 6_200
