"""The Microsoft 365 Copilot alternative: the actions API against the local GitLab stand-in with real CI.

Here the test plays the part of Copilot's model: it reads the MR through the API,
"writes" a test file and drives the job through start / status / retry / bug.
"""

import asyncio
import json
import re
import time
from pathlib import Path

import jwt
import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric import rsa

from devassist.config import Settings
from devassist.dev_gitlab import FakeGitLab, seed_sample_logs
from devassist_actions import package
from devassist_actions.api import create_app
from devassist_actions.auth import AuthConfig, Authenticator, check_bind_is_safe

PAYMENTS = "acme/payments-api"
PROJECT_DIR = Path(__file__).resolve().parents[1] / "m365-copilot"
GOOD_TESTS = '''import pytest

from app.order_service import OrderService


def test_subtotal_adds_price_times_quantity():
    assert OrderService().subtotal([{"price": 10.0, "quantity": 2}, {"price": 5.0, "quantity": 1}]) == 25.0


def test_apply_discount():
    assert OrderService().apply_discount(100.0, 25) == 75.0


@pytest.mark.parametrize("percent", [-1, 101])
def test_apply_discount_rejects_invalid_percent(percent):
    with pytest.raises(ValueError):
        OrderService().apply_discount(100.0, percent)


def test_total_applies_discount_then_tax():
    assert OrderService(tax_rate=0.5).total([{"price": 100.0, "quantity": 1}], discount_percent=50) == 75.0
'''
WRONG_TEST = '''from app.order_service import OrderService


def test_apply_discount():
    assert OrderService().apply_discount(100.0, 25) == 80.0
'''
TENANT, CLIENT = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


class Harness:
    def __init__(self, tmp_path, auth: AuthConfig | None = None, resolve_key=None, **overrides):
        self.tmp_path, self.auth, self.resolve_key, self.overrides = tmp_path, auth or AuthConfig(), resolve_key, overrides

    async def __aenter__(self):
        logs = self.tmp_path / "logs.jsonl"
        seed_sample_logs(logs)
        self.fake = FakeGitLab()
        self.gitlab_runner = web.AppRunner(self.fake.app())
        await self.gitlab_runner.setup()
        site = web.TCPSite(self.gitlab_runner, "127.0.0.1", 0)
        await site.start()
        self.fake.base_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.settings = Settings(
            profile="mock", gitlab_url=self.fake.base_url, gitlab_token="t", gitlab_projects=(PAYMENTS, "acme/customer-portal"),
            log_jsonl_path=logs, audit_path=self.tmp_path / "audit.jsonl", pipeline_poll_seconds=0.1,
            user_map={"dev@acme.test": "dev"}, **self.overrides,
        )
        self.client = TestClient(TestServer(create_app(self.settings, Authenticator(self.auth, self.resolve_key), runs_path=self.tmp_path / "runs.json")))
        await self.client.start_server()
        return self

    async def __aexit__(self, *_):
        await self.client.close()
        await self.gitlab_runner.cleanup()

    async def call(self, method, path, expect=200, headers=None, **kwargs):
        response = await self.client.request(method, path, headers=headers, **kwargs)
        data = await response.json()
        assert response.status == expect, (response.status, data)
        return data

    async def wait_run(self, run_id, until=("passed", "failed", "gave_up", "bug_reported", "error"), headers=None, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            run = await self.call("GET", f"/api/test-runs/{run_id}", headers=headers)
            if run["status"] in until:
                return run
            await asyncio.sleep(0.2)
        raise AssertionError(f"run {run_id} still {run['status']}")

    def ai_mrs(self, project=PAYMENTS):
        return [mr for mr in self.fake.projects[project]["mrs"].values() if mr["source_branch"].startswith("ai/")]


def test_test_loop_runs_real_ci_and_opens_an_mr_only_when_green(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            changes = await h.call("GET", "/api/merge-requests/changes", params={"project": "payments-api", "iid": 231})
            assert changes["source_branch"] == "feature/order-discount"
            assert changes["changed_source_files"][0]["path"] == "app/order_service.py"
            assert "apply_discount" in changes["changed_source_files"][0]["content"]
            assert changes["example_test_path"] == "tests/test_smoke.py"

            started = await h.call("POST", "/api/test-runs", expect=202, json={
                "project": "payments-api", "mergeRequestIid": 231, "label": "OrderService",
                "testFilePath": "tests/test_order_service.py", "testFileContent": GOOD_TESTS})
            assert started["status"] == "starting" and started["branch"].startswith("ai/tests-")
            assert h.ai_mrs() == []  # nothing is proposed before CI says so

            done = await h.wait_run(started["run_id"])
            assert done["status"] == "passed", done
            assert (done["tests_added"], done["tests_passed"], done["tests_failed"]) == (4, 6, 0)
            assert (done["coverage_before"], done["coverage_after"]) == (38.0, 100.0)
            mrs = h.ai_mrs()
            assert len(mrs) == 1 and mrs[0]["state"] == "opened" and mrs[0]["target_branch"] == "feature/order-discount"
            assert done["merge_request_url"] == mrs[0]["web_url"]
    asyncio.run(run())


def test_wrong_test_is_fixed_on_retry(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            started = await h.call("POST", "/api/test-runs", expect=202, json={
                "project": PAYMENTS, "mergeRequestIid": 231, "testFilePath": "tests/test_order_service.py", "testFileContent": WRONG_TEST})
            failed = await h.wait_run(started["run_id"])
            assert failed["status"] == "failed" and failed["attempts_left"] == 2
            assert "80.0" in failed["failure_log"] and "retryTestRun" in failed["next_step"]
            assert h.ai_mrs() == []

            again = await h.call("POST", f"/api/test-runs/{started['run_id']}/attempts", expect=202, json={"testFileContent": GOOD_TESTS})
            assert again["status"] == "running"
            done = await h.wait_run(started["run_id"])
            assert done["status"] == "passed" and done["attempt"] == 2 and len(h.ai_mrs()) == 1
            # A finished run can't be retried.
            await h.call("POST", f"/api/test-runs/{started['run_id']}/attempts", expect=409, json={"testFileContent": GOOD_TESTS})
    asyncio.run(run())


def test_bug_in_code_closes_the_run_without_an_mr(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            started = await h.call("POST", "/api/test-runs", expect=202, json={
                "project": PAYMENTS, "mergeRequestIid": 232, "testFilePath": "tests/test_order_service.py", "testFileContent": GOOD_TESTS})
            failed = await h.wait_run(started["run_id"])
            assert failed["status"] == "failed"
            closed = await h.call("POST", f"/api/test-runs/{started['run_id']}/bug", json={"summary": "apply_discount divides by 10 instead of 100."})
            assert closed["status"] == "bug_reported" and "No MR" in closed["next_step"]
            assert h.ai_mrs() == []
    asyncio.run(run())


def test_attempts_are_capped(tmp_path):
    async def run():
        async with Harness(tmp_path, max_test_attempts=2) as h:
            started = await h.call("POST", "/api/test-runs", expect=202, json={
                "project": PAYMENTS, "mergeRequestIid": 231, "testFilePath": "tests/test_order_service.py", "testFileContent": WRONG_TEST})
            await h.wait_run(started["run_id"])
            await h.call("POST", f"/api/test-runs/{started['run_id']}/attempts", expect=202, json={"testFileContent": WRONG_TEST})
            last = await h.wait_run(started["run_id"], until=("gave_up", "passed", "error"))
            assert last["status"] == "gave_up" and last["attempts_left"] == 0
            await h.call("POST", f"/api/test-runs/{started['run_id']}/attempts", expect=409, json={"testFileContent": GOOD_TESTS})
            assert h.ai_mrs() == []
    asyncio.run(run())


def test_guards_refuse_non_test_paths_other_projects_and_code_changes(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            for path in (".gitlab-ci.yml", "app/order_service.py", "../tests/test_x.py", "tests/notes.md"):
                error = await h.call("POST", "/api/test-runs", expect=400, json={"project": PAYMENTS, "testFilePath": path, "testFileContent": GOOD_TESTS})
                assert "not a test file" in error["error"]
            error = await h.call("GET", "/api/merge-requests", expect=400, params={"project": "acme/secret-service"})
            assert "not enabled" in error["error"]
            changed = (await h.call("GET", "/api/files/content", params={"project": PAYMENTS, "path": "app/order_service.py"}))["content"]
            error = await h.call("POST", "/api/docs/code-comments", expect=400, json={
                "project": PAYMENTS, "path": "app/order_service.py", "content": changed.replace("0.21", "0.5")})
            assert "changes code" in error["error"]
            # A person's test is never overwritten: the AI file gets its own name.
            started = await h.call("POST", "/api/test-runs", expect=202, json={"project": PAYMENTS, "ref": "develop", "testFilePath": "tests/test_smoke.py", "testFileContent": GOOD_TESTS.split("\n\ndef test_apply")[0] + "\n"})
            assert started["path"] == "tests/test_smoke_ai.py"
            await h.wait_run(started["run_id"])
            branches = h.fake.projects[PAYMENTS]["branches"]
            assert all(name.startswith("ai/") or name in ("develop", "feature/order-discount", "feature/buggy-discount") for name in branches)
            assert branches["develop"]["tests/test_smoke.py"].startswith("from app.order_service")
    asyncio.run(run())


def test_api_offers_no_merge_deploy_or_delete(tmp_path):
    """Every route matches the OpenAPI spec, and none of them merges, deploys or deletes."""
    app = create_app(Settings(audit_path=tmp_path / "audit.jsonl"), Authenticator(AuthConfig()), runs_path=tmp_path / "runs.json")
    spec = yaml.safe_load((PROJECT_DIR / "appPackage" / "apiSpecificationFile" / "devassist-actions.yml").read_text(encoding="utf-8"))
    documented = {(method.upper(), "/api" + path, op["operationId"]) for path, item in spec["paths"].items() for method, op in item.items()}
    routes = {(r.method, r.resource.canonical, r.name) for r in app.router.routes() if r.name and r.method != "HEAD"}
    assert routes == documented
    assert not any(method == "DELETE" or re.search(r"merge$|deploy|delete|approve", path + (name or ""), re.I) for method, path, name in routes)


def test_docs_are_proposed_as_merge_requests(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            files = await h.call("GET", "/api/files", params={"project": "customer-portal", "pathContains": "controller"})
            assert "controllers/customers.py" in files["files"]
            reference = await h.call("POST", "/api/docs/api-reference", expect=201, json={"project": "customer-portal", "content": "# API\n\n## GET /customers\n"})
            assert reference["files"] == ["README.md", "docs/api.md"]
            changelog = await h.call("POST", "/api/docs/changelog", expect=201, json={"project": PAYMENTS})
            assert "2 merged MR" in changelog["summary"]
            original = (await h.call("GET", "/api/files/content", params={"project": PAYMENTS, "path": "app/order_service.py"}))["content"]
            commented = original.replace("class OrderService:\n", 'class OrderService:\n    """Prices orders."""\n\n', 1)
            await h.call("POST", "/api/docs/code-comments", expect=201, json={"project": PAYMENTS, "path": "app/order_service.py", "content": commented})
            assert len(h.ai_mrs("acme/customer-portal")) == 1 and len(h.ai_mrs()) == 2
            assert all(mr["state"] == "opened" for mr in h.ai_mrs() + h.ai_mrs("acme/customer-portal"))
    asyncio.run(run())


def test_trace_facts_are_counted_by_code_and_masked(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            facts = await h.call("GET", "/api/incidents", params={"question": "Why did checkout fail yesterday between 17:00 and 18:00?", "project": "payments-api"})
            assert facts["service"] == "checkout" and facts["total_errors"] == 49
            assert facts["error_groups"][0]["count"] == 42
            text = json.dumps(facts)
            assert "@example.com" not in text and "10.0.4." not in text and "C9000" not in text
            assert facts["deployments_in_window"][0]["sha"] == "9f3c2a71"
            red = await h.call("GET", "/api/pipelines/failure", params={"project": PAYMENTS, "pipelineId": 5512})
            assert red["job_name"] == "unit-tests" and "ModuleNotFoundError" in red["log_tail"]
            branch = await h.call("POST", "/api/branches", expect=201, json={"project": PAYMENTS, "branch": "feature/invoice-pdf", "ref": "develop"})
            assert branch["branch"] == "feature/invoice-pdf"
            issue = await h.call("POST", "/api/issues", expect=201, json={"project": PAYMENTS, "title": "checkout: DB pool exhausted", "description": "Root cause..."})
            assert issue["iid"] == 1
    asyncio.run(run())


# ---------------------------------------------------------------- authentication

def _key_and_token_factory():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def token(**claims):
        now = int(time.time())
        body = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "tid": TENANT, "aud": f"api://{CLIENT}", "scp": "DevAssist.Use",
                "oid": "user-1", "name": "Dev One", "preferred_username": "dev@acme.test", "iat": now, "nbf": now, "exp": now + 600, **claims}
        return {"Authorization": "Bearer " + jwt.encode(body, key, algorithm="RS256")}

    async def resolve(_token):
        return key.public_key()

    return token, resolve


def test_entra_token_is_validated_and_identity_used(tmp_path):
    token, resolve = _key_and_token_factory()
    auth = AuthConfig(tenant_id=TENANT, audiences=(f"api://{CLIENT}", CLIENT))

    async def run():
        async with Harness(tmp_path, auth=auth, resolve_key=resolve) as h:
            await h.call("GET", "/api/projects", expect=401)
            await h.call("GET", "/api/projects", expect=401, headers={"Authorization": "Bearer not-a-jwt"})
            await h.call("GET", "/api/projects", expect=401, headers=token(aud="api://someone-else"))
            await h.call("GET", "/api/projects", expect=401, headers=token(exp=int(time.time()) - 3600))
            guest = "33333333-3333-4333-8333-333333333333"
            await h.call("GET", "/api/projects", expect=403, headers=token(tid=guest, iss=f"https://login.microsoftonline.com/{guest}/v2.0"))
            await h.call("GET", "/api/projects", expect=403, headers=token(scp="User.Read"))

            mine = await h.call("GET", "/api/merge-requests", headers=token(), params={"project": PAYMENTS, "assignedToMe": "true"})
            assert mine["assignee"] == "dev" and [mr["iid"] for mr in mine["merge_requests"]] == [231]

            started = await h.call("POST", "/api/test-runs", expect=202, headers=token(), json={
                "project": PAYMENTS, "mergeRequestIid": 231, "testFilePath": "tests/test_order_service.py", "testFileContent": GOOD_TESTS})
            await h.call("GET", f"/api/test-runs/{started['run_id']}", expect=404, headers=token(oid="user-2"))  # not your run
            await h.wait_run(started["run_id"], headers=token())
        audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        assert any(r["kind"] == "action" and r["operation"] == "startTestRun" and r["user"] == "Dev One" for r in audit)
        assert any(r["kind"] == "action_denied" and r["status"] == 403 for r in audit)
        assert not any("Bearer" in json.dumps(r) for r in audit)
    asyncio.run(run())


def test_anonymous_mode_only_on_loopback():
    check_bind_is_safe("127.0.0.1", AuthConfig())
    with pytest.raises(RuntimeError):
        check_bind_is_safe("0.0.0.0", AuthConfig())
    check_bind_is_safe("0.0.0.0", AuthConfig(tenant_id=TENANT, audiences=(CLIENT,)))
    with pytest.raises(ValueError):
        AuthConfig.from_env({"DEVASSIST_ACTIONS_TENANT_ID": TENANT})


def test_runs_survive_a_restart(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            started = await h.call("POST", "/api/test-runs", expect=202, json={
                "project": PAYMENTS, "mergeRequestIid": 231, "testFilePath": "tests/test_order_service.py", "testFileContent": GOOD_TESTS})
            await h.wait_run(started["run_id"])
            stored = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
            # Pretend the service died while CI was running.
            stored[0]["status"] = "running"
            (tmp_path / "runs.json").write_text(json.dumps(stored), encoding="utf-8")
            await h.client.close()
            h.client = TestClient(TestServer(create_app(h.settings, Authenticator(AuthConfig()), runs_path=tmp_path / "runs.json")))
            await h.client.start_server()
            done = await h.wait_run(started["run_id"], until=("passed",))
            assert done["status"] == "passed"
    asyncio.run(run())


# ---------------------------------------------------------------- app package

def test_package_follows_copilot_rules_offline():
    files, problems = package.rendered_files(PROJECT_DIR, "local")
    assert problems == []
    manifest, agent, plugin = (json.loads(files[n]) for n in ("manifest.json", "declarativeAgent.json", "ai-plugin.json"))
    spec = yaml.safe_load(files[package.SPEC_PATH])
    assert package.copilot_rules(manifest, agent, plugin, spec, files) == []
    assert "Test specialist" in agent["instructions"] and len(agent["instructions"]) <= package.MAX_INSTRUCTIONS


@pytest.mark.skipif(not (PROJECT_DIR / ".schemas" / "plugin-2.4.json").exists(), reason="run `devassist-actions validate` once to download the official schemas")
def test_package_matches_official_schemas():
    assert package.validate_project(PROJECT_DIR, "local", download=False) == []
