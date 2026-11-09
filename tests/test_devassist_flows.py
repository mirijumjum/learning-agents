"""End-to-end flows against the local GitLab stand-in, with real pytest runs as CI."""

import asyncio
from dataclasses import replace

import pytest
from aiohttp import web

from devassist.audit import AuditLog, RequestContext, token_cost
from devassist.config import Settings
from devassist.dev_gitlab import FakeGitLab, seed_sample_logs
from devassist.evals import run_evals
from devassist.runtime import build_devassist
from devassist.specialists import AgentRefusal, TestAgent

PAYMENTS = "acme/payments-api"


class Harness:
    def __init__(self, tmp_path, **overrides):
        self.tmp_path = tmp_path
        self.overrides = overrides

    async def __aenter__(self):
        logs = self.tmp_path / "logs.jsonl"
        seed_sample_logs(logs)
        self.fake = FakeGitLab()
        self.runner = web.AppRunner(self.fake.app())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.fake.base_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.settings = Settings(
            profile="mock", gitlab_url=self.fake.base_url, gitlab_token="t", gitlab_projects=(PAYMENTS, "acme/customer-portal"),
            log_jsonl_path=logs, audit_path=self.tmp_path / "audit.jsonl", pipeline_poll_seconds=0.1, **self.overrides,
        )
        self.assistant = build_devassist(self.settings)
        return self

    async def __aexit__(self, *_):
        await self.assistant.gitlab.aclose()
        await self.runner.cleanup()

    async def ask(self, text, conversation="c1"):
        return await self.assistant.handle(text, RequestContext(user="dev", email="dev@acme.test", conversation_id=conversation, source="test"))

    def ai_mrs(self, project=PAYMENTS):
        return [mr for mr in self.fake.projects[project]["mrs"].values() if mr["source_branch"].startswith("ai/")]


def test_test_agent_runs_ci_and_opens_an_mr(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            reply = await h.ask("Generate unit tests for `OrderService` in MR !231 of `payments-api`")
            # 4 functions in the new file; the pipeline also runs the repo's existing smoke test.
            assert "4 tests added. Pipeline: 6 passing, 0 failing." in reply.text
            assert "Coverage 38% → 100%" in reply.text
            mrs = h.ai_mrs()
            assert len(mrs) == 1 and mrs[0]["source_branch"].startswith("ai/tests-") and mrs[0]["target_branch"] == "feature/order-discount"
            assert mrs[0]["state"] == "opened"  # proposed, never merged
            assert "tests/test_order_service.py" in h.fake.projects[PAYMENTS]["branches"][mrs[0]["source_branch"]]
            assert "tests/test_order_service.py" not in h.fake.projects[PAYMENTS]["branches"]["feature/order-discount"]

    asyncio.run(run())


def test_test_agent_fixes_its_own_wrong_test(tmp_path):
    async def run():
        async with Harness(tmp_path, mock_flaky_tests=True) as h:
            reply = await h.ask("Generate unit tests for MR !231 of `payments-api`")
            assert "6 passing" in reply.text
            ran = [p for p in h.fake.pipelines.values() if p["ref"].startswith("ai/tests-")]
            assert [p["status"] for p in sorted(ran, key=lambda p: p["id"])] == ["failed", "success"]

    asyncio.run(run())


def test_test_agent_stops_and_reports_a_real_bug(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            reply = await h.ask("Generate unit tests for MR !232 of `payments-api`")
            assert "bug in the code" in reply.text and "divides the percentage by 10" in reply.text
            assert h.ai_mrs() == []  # no MR for a failing suite

    asyncio.run(run())


@pytest.mark.parametrize("reply", [
    "FILE: tests/test_a.py\n```python\nx = 1\n```",
    "FILE: `tests/test_a.py`\n```\nx = 1\n```",
    "**FILE:** tests/test_a.py\n```python\nx = 1\n```",
    "Here you go:\n\nFILE: tests/test_a.py\n\n```py\nx = 1\n```\nHope it helps",
])
def test_reasonable_reply_formats_are_understood(reply):
    assert TestAgent._parse_file(reply) == ("tests/test_a.py", "x = 1\n")


def test_badly_formatted_reply_gets_one_retry_and_then_a_clear_refusal(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            calls = []

            async def always_prose(role, instructions_text, prompt, tools=None, deliberate=False):
                calls.append(prompt)
                return "Sure! Here are some thoughts about testing."

            h.assistant.llm.ask = always_prose
            reply = await h.assistant.test.run(PAYMENTS, mr_iid=231)

    with pytest.raises(AgentRefusal, match="expected format"):
        asyncio.run(run())


def test_a_runaway_model_call_is_stopped_with_a_clear_message(tmp_path):
    async def run():
        async with Harness(tmp_path, llm_timeout_seconds=0.05) as h:
            client = h.assistant.llm._client("router")
            original = client._inner_get_response

            async def slow(**kwargs):
                await asyncio.sleep(1)
                return await original(**kwargs)

            client._inner_get_response = slow
            reply = await h.ask("hello")
            assert "took longer than" in reply.text

    asyncio.run(run())


def test_oversized_diff_is_rejected_before_any_model_call(tmp_path):
    async def run():
        async with Harness(tmp_path, max_diff_chars=10) as h:
            reply = await h.ask("Generate unit tests for MR !231 of `payments-api`")
            assert "limit" in reply.text
            assert h.ai_mrs() == []
            models = [l for l in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines() if '"role": "test"' in l]
            assert models == []

    asyncio.run(run())


def test_git_agent_acts_and_removes_invented_links(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            reply = await h.ask("Create a branch `feature/invoice-pdf` from `develop` in `payments-api` and list open MRs assigned to me")
            assert "feature/invoice-pdf" in h.fake.projects[PAYMENTS]["branches"]
            rendered = reply.render()
            assert "!231" in rendered and "!232" not in rendered  # 232 is assigned to someone else
            assert "gitlab.example.com" not in rendered

    asyncio.run(run())


def test_trace_counts_are_measured_and_logs_masked(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            reply = await h.ask("Why did checkout fail yesterday between 17:00 and 18:00 in `payments-api`?")
            assert "49 errors in" in reply.text and "42x" in reply.text
            assert "jane.doe" not in reply.text and "10.0.4." not in reply.text

    asyncio.run(run())


def test_trace_offer_then_open_issue_in_same_conversation(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            first = await h.ask("Why is pipeline #5512 of `payments-api` red?", conversation="a")
            assert "open the issue" in first.text
            created = await h.ask("yes, open the issue", conversation="a")
            assert "Issue #1 created" in created.text
            assert h.fake.projects[PAYMENTS]["issues"][0]["title"].startswith("Pipeline #5512")

    asyncio.run(run())


def test_issue_draft_does_not_leak_between_conversations(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            await h.ask("Why is pipeline #5512 of `payments-api` red?", conversation="a")
            await h.ask("open the issue", conversation="b")
            assert h.fake.projects[PAYMENTS]["issues"] == []

    asyncio.run(run())


def test_docs_agent_opens_an_mr_with_api_reference_and_readme_link(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            reply = await h.ask("Document the public endpoints of `customer-portal` and open an MR")
            assert "docs/api.md" in reply.text
            branch = h.ai_mrs("acme/customer-portal")[0]["source_branch"]
            files = h.fake.projects["acme/customer-portal"]["branches"][branch]
            assert "GET /customers" in files["docs/api.md"] and "docs/api.md" in files["README.md"]

    asyncio.run(run())


def test_changelog_lists_real_merged_mrs(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            await h.ask("Update the changelog of `payments-api`")
            branch = h.ai_mrs()[0]["source_branch"]
            changelog = h.fake.projects[PAYMENTS]["branches"][branch]["CHANGELOG.md"]
            assert "Fix rounding of totals (!220)" in changelog and "Add tax rate setting (!218)" in changelog

    asyncio.run(run())


def test_code_comments_only_change_documentation(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            reply = await h.ask("Add code comments to `customer-portal`")
            assert "documentation comments" in reply.text
            branch = h.ai_mrs("acme/customer-portal")[0]["source_branch"]
            assert '"""Return all customers."""' in h.fake.projects["acme/customer-portal"]["branches"][branch]["controllers/customers.py"]

    asyncio.run(run())


def test_every_request_tool_and_token_is_audited(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            await h.ask("Why is pipeline #5512 of `payments-api` red?")
            await h.ask("feedback bad too slow for a@b.com")
            kinds = [l for l in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
            for needle in ('"kind": "request_start"', '"kind": "llm"', '"kind": "tool"', '"kind": "request_end"', '"kind": "feedback"'):
                assert any(needle in line for line in kinds), needle
            assert not any("a@b.com" in line for line in kinds)
            summary = h.assistant.audit.summary()
            assert summary["requests"] == 2 and summary["feedback_bad"] == 1
            assert summary["cost_usd"] == 0 and summary["estimated_azure_cost_usd"] > 0

    asyncio.run(run())


def test_token_cost_uses_design_prices():
    assert token_cost("strong", 1_000_000, 1_000_000) == pytest.approx(12.375)
    assert token_cost("fast", 1_000_000, 1_000_000) == pytest.approx(2.475)


def test_release_evals_pass_on_mock(tmp_path):
    settings = replace(Settings(profile="mock"), audit_path=tmp_path / "a.jsonl")
    results = asyncio.run(run_evals(settings))
    assert [r.detail for r in results if not r.passed] == []
    assert len(results) >= 6
