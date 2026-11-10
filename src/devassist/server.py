"""The web service: Teams messages on /api/messages, GitLab events on /webhooks/gitlab.

Locally there are no credentials and it runs anonymously, on loopback only. With
CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID set, every Teams request must
carry a valid Bot Service token.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from aiohttp import web
from aiohttp.web import Application, Request, Response, json_response
from microsoft_agents.activity import Activity, ActivityTypes, load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter, jwt_authorization_middleware, start_agent_process
from microsoft_agents.hosting.core import AgentApplication, AgentAuthConfiguration, Authorization, MemoryStorage, TurnContext, TurnState

from devassist.audit import RequestContext, configure_telemetry
from devassist.automations import Automations
from devassist.orchestrator import DevAssist
from devassist.webhooks import check_gitlab_secret, delivery_id

logger = logging.getLogger("devassist.server")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_CLIENT_ID_VAR = "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID"
AUTOMATIONS_KEY = web.AppKey("automations", Automations)
ERROR_TEXT = "Sorry, something went wrong on my side. Please try again, or tell the DevAssist owner if it keeps happening."


def is_authenticated_mode(env: dict[str, str] | None = None) -> bool:
    return bool((env if env is not None else os.environ).get(_CLIENT_ID_VAR))


def check_bind_is_safe(host: str) -> None:
    """Without Bot Service credentials anyone reaching the port could use the agents."""
    if not is_authenticated_mode() and host not in _LOOPBACK:
        raise RuntimeError(f"Refusing to listen on {host} without authentication. Set {_CLIENT_ID_VAR}, or use 127.0.0.1.")


def _tenant_allowed(activity: Activity, allowed_tenant: str) -> bool:
    """Only members of the company tenant may use the agents, so guests are rejected."""
    if not allowed_tenant:
        return True
    data = activity.channel_data if isinstance(activity.channel_data, dict) else {}
    tenant = (data.get("tenant") or {}).get("id") or getattr(activity.conversation, "tenant_id", None)
    return tenant == allowed_tenant


def build_agent_application(
    devassist: DevAssist, env: dict[str, str] | None = None, connections: Any = None, deferred_replies: bool | None = None,
) -> tuple[AgentApplication, CloudAdapter, AgentAuthConfiguration]:
    """deferred_replies: acknowledge at once and answer later. On by default with credentials, since it needs a token."""
    variables = dict(env if env is not None else os.environ)
    config = load_configuration_from_env(variables)
    authenticated = is_authenticated_mode(variables)
    deferred = authenticated if deferred_replies is None else deferred_replies
    storage = MemoryStorage()
    if connections is None:
        if authenticated:
            connections = MsalConnectionManager(**config)
        else:
            connections = MsalConnectionManager(connections_configurations={"SERVICE_CONNECTION": AgentAuthConfiguration(anonymous_allowed=True)})
    adapter = CloudAdapter(connection_manager=connections)
    authorization = Authorization(storage, connections, **config)
    application: AgentApplication = AgentApplication[TurnState](storage=storage, authorization=authorization, **config)
    auth_config = connections.get_default_connection_configuration()
    app_id = (auth_config.CLIENT_ID if auth_config else "") or ""
    tasks: set[asyncio.Task] = set()

    async def produce(text: str, request: RequestContext) -> str:
        try:
            return (await devassist.handle(text, request)).render()
        except Exception:
            logger.exception("Request %s failed", request.request_id)
            return ERROR_TEXT

    async def work(reference: Activity, text: str, request: RequestContext) -> None:
        """Runs after the Teams request is acknowledged. A test run can take minutes, Teams allows seconds."""
        answer = await produce(text, request)

        async def deliver(context: TurnContext) -> None:
            await context.send_activity(answer)

        try:
            await adapter.continue_conversation(app_id, reference, deliver)
        except Exception:
            logger.exception("Could not deliver the reply to request %s", request.request_id)

    @application.activity(ActivityTypes.message)
    async def on_message(context: TurnContext, _state: TurnState) -> None:
        activity = context.activity
        if not _tenant_allowed(activity, devassist.settings.allowed_tenant_id):
            await context.send_activity("DevAssist is only available to members of this organisation.")
            return
        activity.remove_recipient_mention()
        text = (activity.text or "").strip()
        if not text:
            return
        await context.send_activity("On it…")

        person = activity.from_property
        request = RequestContext(
            user=(person.name if person else None) or "unknown",
            email=(getattr(person, "user_principal_name", None) or "") if person else "",
            conversation_id=activity.conversation.id,
            source="teams",
        )
        if not deferred:
            await context.send_activity(await produce(text, request))
            return
        reference = activity.get_conversation_reference().get_continuation_activity()
        task = asyncio.create_task(work(reference, text, request))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    return application, adapter, auth_config


def create_app(devassist: DevAssist, env: dict[str, str] | None = None, connections: Any = None, deferred_replies: bool | None = None) -> Application:
    settings = devassist.settings
    configure_telemetry(settings.appinsights_connection_string)
    application, adapter, auth_config = build_agent_application(devassist, env, connections, deferred_replies)
    automations = Automations(devassist)

    async def messages(request: Request) -> Response:
        response = await start_agent_process(request, application, adapter)
        return response if response is not None else Response(status=202)

    async def gitlab_webhook(request: Request) -> Response:
        rejected = check_gitlab_secret(request.headers, settings.gitlab_webhook_secret)
        if rejected:
            return json_response({"error": rejected[1]}, status=rejected[0])
        if automations.is_duplicate(delivery_id(request.headers)):
            return json_response({"status": "duplicate"}, status=200)
        try:
            payload: dict[str, Any] = await request.json()
        except ValueError:
            return json_response({"error": "invalid JSON"}, status=400)
        plan = automations.plan(request.headers.get("X-Gitlab-Event", ""), payload)
        if plan is None:
            return json_response({"status": "ignored"}, status=200)
        automations.start(*plan)
        return json_response({"status": "accepted", "action": plan[0]}, status=202)

    async def health(_: Request) -> Response:
        return json_response({"status": "ok", "profile": settings.profile, "authenticated": is_authenticated_mode(env)})

    # Only the Teams route needs a Bot Service token; webhooks have their own secret.
    @web.middleware
    async def scoped_auth(request: Request, handler: Any) -> Response:
        if request.path.startswith("/api/messages"):
            return await jwt_authorization_middleware(request, handler)
        return await handler(request)

    app = Application(middlewares=[scoped_auth])
    app["agent_configuration"] = auth_config  # the SDK's middleware reads this exact key
    app[AUTOMATIONS_KEY] = automations
    app.add_routes([web.post("/api/messages", messages), web.post("/webhooks/gitlab", gitlab_webhook), web.get("/healthz", health)])
    return app
