"""Automation results posted to a Teams channel through a Workflows webhook."""

import asyncio

from aiohttp import web

from devassist import cli
from devassist.automations import Automations, teams_card
from devassist.server import AUTOMATIONS_KEY, create_app
from test_devassist_flows import PAYMENTS, Harness


async def _serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def _fake_teams(status=202):
    """Stands in for the Workflows trigger URL, which answers 202 Accepted."""
    received = []

    async def trigger(request):
        received.append(await request.json())
        return web.Response(status=status)

    app = web.Application()
    app.router.add_post("/workflows/trigger", trigger)
    runner, url = await _serve(app)
    return runner, f"{url}/workflows/trigger", received


def _card_texts(body):
    assert body["type"] == "message"
    (attachment,) = body["attachments"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    return [block["text"] for block in card["body"]]


def test_card_has_the_shape_workflows_expects_and_is_truncated():
    title, text = _card_texts(teams_card("Title", "x" * 30000))
    assert title == "Title"
    assert len(text) < 21000 and text.endswith("(truncated)")


def test_simulated_pipeline_failure_ends_up_as_a_card_in_teams(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep the developer's own .env.devassist out of the test
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "s3cret")

    async def run():
        teams_runner, teams_url, received = await _fake_teams()
        async with Harness(tmp_path, gitlab_webhook_secret="s3cret", auto_trace_on_pipeline_failure=True, teams_notify_url=teams_url) as h:
            app = create_app(h.assistant, env={})
            runner, url = await _serve(app)
            code = await asyncio.to_thread(cli.main, ["simulate-webhook", "pipeline-failed", "--url", url, "--profile", "mock"])
            assert code == 0
            while app[AUTOMATIONS_KEY]._tasks:
                await asyncio.sleep(0.05)
            await runner.cleanup()
        await teams_runner.cleanup()
        assert len(received) == 1
        title, text = _card_texts(received[0])
        assert "failed pipeline" in title and PAYMENTS in title
        assert "open the issue" not in text

    asyncio.run(run())


def test_a_teams_outage_does_not_fail_the_automation(tmp_path):
    async def run():
        teams_runner, teams_url, received = await _fake_teams(status=500)
        async with Harness(tmp_path, auto_trace_on_pipeline_failure=True, teams_notify_url=teams_url) as h:
            reply = await Automations(h.assistant).execute("trace", {"project": PAYMENTS, "pipeline_id": 5512})
            assert reply.render()
        await teams_runner.cleanup()
        assert len(received) == 1

    asyncio.run(run())


def test_notify_teams_command_needs_a_url(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEAMS_NOTIFY_WEBHOOK_URL", raising=False)
    assert cli.main(["notify-teams", "--profile", "mock"]) == 2
    assert "TEAMS_NOTIFY_WEBHOOK_URL" in capsys.readouterr().err
