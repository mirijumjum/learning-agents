"""The Teams endpoint and GitLab webhooks, over real HTTP."""

import asyncio
import json
import zipfile

import pytest
from aiohttp import ClientSession, web
from microsoft_agents.hosting.core import AgentAuthConfiguration
from microsoft_agents.hosting.core.authorization.connection_manager import ConnectionManager

from devassist.automations import Automations
from devassist.server import AUTOMATIONS_KEY, check_bind_is_safe, create_app
from devassist.teams_package import build_manifest, write_package
from test_devassist_flows import PAYMENTS, Harness

BOT_ID = "11111111-2222-3333-4444-555555555555"


async def _serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def _webhook_headers(secret="s3cret", event="Merge Request Hook", delivery="d1"):
    return {"X-Gitlab-Token": secret, "X-Gitlab-Event": event, "Idempotency-Key": delivery}


def _mr_event(action="open", source="feature/order-discount"):
    return {"project": {"path_with_namespace": PAYMENTS}, "object_attributes": {"action": action, "iid": 231, "source_branch": source, "target_branch": "develop"}}


async def _settle(app):
    while app[AUTOMATIONS_KEY]._tasks:
        await asyncio.sleep(0.05)


def test_webhook_rejects_wrong_secret_and_is_off_without_one(tmp_path):
    async def run():
        async with Harness(tmp_path, gitlab_webhook_secret="s3cret") as h:
            runner, url = await _serve(create_app(h.assistant, env={}))
            async with ClientSession() as http:
                bad = await http.post(f"{url}/webhooks/gitlab", json=_mr_event(), headers=_webhook_headers(secret="nope"))
                assert bad.status == 401
            await runner.cleanup()
        async with Harness(tmp_path) as h:
            runner, url = await _serve(create_app(h.assistant, env={}))
            async with ClientSession() as http:
                off = await http.post(f"{url}/webhooks/gitlab", json=_mr_event(), headers=_webhook_headers())
                assert off.status == 503
            await runner.cleanup()

    asyncio.run(run())


def test_opened_mr_gets_tests_and_a_comment_once(tmp_path):
    async def run():
        async with Harness(tmp_path, gitlab_webhook_secret="s3cret", auto_tests_on_mr=True) as h:
            app = create_app(h.assistant, env={})
            runner, url = await _serve(app)
            async with ClientSession() as http:
                first = await http.post(f"{url}/webhooks/gitlab", json=_mr_event(), headers=_webhook_headers())
                assert first.status == 202
                retry = await http.post(f"{url}/webhooks/gitlab", json=_mr_event(), headers=_webhook_headers())
                assert (await retry.json())["status"] == "duplicate"
            await _settle(app)
            notes = h.fake.projects[PAYMENTS]["notes"]
            assert len(notes) == 1 and notes[0]["mr"] == 231 and "tests added" in notes[0]["body"]
            assert len(h.ai_mrs()) == 1
            await runner.cleanup()

    asyncio.run(run())


def test_automations_are_opt_in(tmp_path):
    async def run():
        async with Harness(tmp_path, gitlab_webhook_secret="s3cret") as h:
            runner, url = await _serve(create_app(h.assistant, env={}))
            async with ClientSession() as http:
                response = await http.post(f"{url}/webhooks/gitlab", json=_mr_event(), headers=_webhook_headers())
                assert (await response.json())["status"] == "ignored"
            await runner.cleanup()

    asyncio.run(run())


def test_agents_do_not_trigger_themselves(tmp_path):
    async def run():
        async with Harness(tmp_path, auto_tests_on_mr=True, auto_trace_on_pipeline_failure=True) as h:
            automations = Automations(h.assistant)
            assert automations.plan("Merge Request Hook", _mr_event(source="ai/tests-x")) is None
            pipeline = {"project": {"path_with_namespace": PAYMENTS}, "object_attributes": {"id": 1, "status": "failed", "ref": "ai/tests-x"}}
            assert automations.plan("Pipeline Hook", pipeline) is None
            pipeline["object_attributes"]["ref"] = "develop"
            assert automations.plan("Pipeline Hook", pipeline)[0] == "trace"

    asyncio.run(run())


def _activity(service_url, text="<at>DevAssist</at> help", tenant="tenant-a"):
    return {
        "type": "message", "id": "1", "channelId": "emulator", "serviceUrl": service_url, "text": text,
        "from": {"id": "user1", "name": "Dev"}, "recipient": {"id": "bot1", "name": "DevAssist"},
        "conversation": {"id": "conv1", "tenantId": tenant}, "channelData": {"tenant": {"id": tenant}},
        "entities": [{"type": "mention", "text": "<at>DevAssist</at>", "mentioned": {"id": "bot1", "name": "DevAssist"}}],
    }


async def _connector():
    """Stands in for the Bot Connector service that Teams or Agents Playground provide."""
    received = []

    async def activities(request):
        received.append(await request.json())
        return web.json_response({"id": str(len(received))})

    app = web.Application()
    app.router.add_post("/v3/conversations/{conversation}/activities", activities)
    app.router.add_post("/v3/conversations/{conversation}/activities/{activity}", activities)
    runner, url = await _serve(app)
    return runner, url, received


async def _wait_for(received, count, seconds=10):
    for _ in range(int(seconds / 0.05)):
        if len(received) >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {count} replies, got {received}")


def test_teams_message_is_acknowledged_then_answered(tmp_path):
    async def run():
        async with Harness(tmp_path) as h:
            connector, connector_url, received = await _connector()
            runner, url = await _serve(create_app(h.assistant, env={}))
            async with ClientSession() as http:
                response = await http.post(f"{url}/api/messages", json=_activity(connector_url))
                assert response.status in (200, 201, 202)
            await _wait_for(received, 2)
            assert received[0]["text"] == "On it…"
            assert "DevAssist" in received[1]["text"] and "never merge" in received[1]["text"]
            await runner.cleanup()
            await connector.cleanup()

    asyncio.run(run())


class _StubTokenProvider:
    def __init__(self, _config):
        pass

    async def get_access_token(self, resource_url, scopes, force_refresh=False):
        return "stub-token"


def test_deferred_reply_arrives_after_the_request_has_returned(tmp_path):
    """The path real Teams uses: acknowledge at once, deliver the answer in a later call."""

    async def run():
        config = AgentAuthConfiguration(client_id=BOT_ID, tenant_id="22222222-2222-2222-2222-222222222222", anonymous_allowed=True)
        connections = ConnectionManager(provider_factory=_StubTokenProvider, connections_configurations={"SERVICE_CONNECTION": config})
        async with Harness(tmp_path) as h:
            connector, connector_url, received = await _connector()
            runner, url = await _serve(create_app(h.assistant, env={}, connections=connections, deferred_replies=True))
            async with ClientSession() as http:
                response = await http.post(f"{url}/api/messages", json=_activity(connector_url))
                assert response.status in (200, 201, 202)
            await _wait_for(received, 2)
            assert received[0]["text"] == "On it…" and "never merge" in received[1]["text"]
            await runner.cleanup()
            await connector.cleanup()

    asyncio.run(run())


def test_other_tenants_are_refused(tmp_path):
    async def run():
        async with Harness(tmp_path, allowed_tenant_id="tenant-a") as h:
            connector, connector_url, received = await _connector()
            runner, url = await _serve(create_app(h.assistant, env={}))
            async with ClientSession() as http:
                await http.post(f"{url}/api/messages", json=_activity(connector_url, tenant="guest-tenant"))
            await _wait_for(received, 1)
            await asyncio.sleep(0.2)
            assert len(received) == 1 and "only available" in received[0]["text"]
            await runner.cleanup()
            await connector.cleanup()

    asyncio.run(run())


def test_authenticated_mode_rejects_requests_without_a_token(tmp_path):
    env = {
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": BOT_ID,
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": "22222222-2222-2222-2222-222222222222",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE": "UserManagedIdentity",
    }

    async def run():
        async with Harness(tmp_path) as h:
            runner, url = await _serve(create_app(h.assistant, env=env))
            async with ClientSession() as http:
                response = await http.post(f"{url}/api/messages", json=_activity("http://127.0.0.1:1"))
                assert response.status == 401
            await runner.cleanup()

    asyncio.run(run())


def test_unauthenticated_server_refuses_public_interfaces(monkeypatch):
    monkeypatch.delenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", raising=False)
    with pytest.raises(RuntimeError, match="Refusing"):
        check_bind_is_safe("0.0.0.0")
    check_bind_is_safe("127.0.0.1")


def _manifest():
    return build_manifest(BOT_ID, "Acme", "https://acme.test", "https://acme.test/privacy", "https://acme.test/terms")


def test_teams_package_contains_manifest_and_valid_icons(tmp_path):
    from PIL import Image

    path = write_package(_manifest(), tmp_path / "app.zip")
    with zipfile.ZipFile(path) as archive:
        assert sorted(archive.namelist()) == ["color.png", "manifest.json", "outline.png"]
        manifest = json.loads(archive.read("manifest.json"))
        with archive.open("color.png") as icon:
            assert Image.open(icon).size == (192, 192)
        with archive.open("outline.png") as icon:
            assert Image.open(icon).size == (32, 32)
    assert manifest["bots"][0]["botId"] == BOT_ID == manifest["copilotAgents"]["customEngineAgents"][0]["id"]


def test_manifest_app_id_is_stable_for_updates():
    assert _manifest()["id"] == _manifest()["id"]


@pytest.mark.parametrize("bad", [{"bot_id": "not-a-guid"}, {"website": "http://insecure.test"}])
def test_manifest_rejects_bad_input(bad):
    values = {"bot_id": BOT_ID, "developer": "Acme", "website": "https://a.test", "privacy_url": "https://a.test/p", "terms_url": "https://a.test/t", **bad}
    with pytest.raises(ValueError):
        build_manifest(**values)
