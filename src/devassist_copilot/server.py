"""HTTP service that Copilot Studio calls: MCP on /mcp, REST on /api/tools/<Tool>.

Authentication modes (DEVASSIST_COPILOT_AUTH):
- none: local development only. Refuses to listen anywhere but loopback.
- apikey: every call needs X-API-Key. Who is asking comes from the
  X-DevAssist-Requester header, which the maker sets to System.User.Email.
- easyauth: the app runs behind Azure Container Apps / App Service built-in
  authentication with Microsoft Entra ID (the connector uses OAuth 2.0). The
  platform validates the token and injects X-MS-CLIENT-PRINCIPAL-NAME, which is
  the user. The app must not be reachable except through that front door.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from aiohttp import web

from devassist.audit import AuditLog
from devassist.config import Settings
from devassist.gitlab import GitLabClient
from devassist.logstore import make_log_store
from devassist_copilot.mcp import handle_message
from devassist_copilot.openapi import API_KEY_HEADER, REQUESTER_HEADER, build_mcp_spec, build_rest_spec
from devassist_copilot.sessions import SessionStore
from devassist_copilot.tools import TOOLS_BY_NAME, Backend, Caller, ToolError

logger = logging.getLogger("devassist_copilot.server")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
AUTH_MODES = ("none", "apikey", "easyauth")
EASYAUTH_PRINCIPAL = "X-MS-CLIENT-PRINCIPAL-NAME"
DEFAULT_PORT = 3981
BACKEND_KEY = web.AppKey("backend", Backend)


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "none"
    api_key: str = ""
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AuthConfig":
        e = os.environ if env is None else env
        key = e.get("DEVASSIST_COPILOT_API_KEY", "")
        mode = e.get("DEVASSIST_COPILOT_AUTH") or ("apikey" if key else "none")
        origins = tuple(o.strip() for o in e.get("DEVASSIST_COPILOT_ALLOWED_ORIGINS", "").split(",") if o.strip())
        config = cls(mode, key, origins)
        config.validate()
        return config

    def validate(self) -> None:
        if self.mode not in AUTH_MODES:
            raise RuntimeError(f"DEVASSIST_COPILOT_AUTH must be one of {', '.join(AUTH_MODES)}, got {self.mode!r}.")
        if self.mode == "apikey" and len(self.api_key) < 24:
            raise RuntimeError("DEVASSIST_COPILOT_AUTH=apikey needs DEVASSIST_COPILOT_API_KEY of at least 24 characters.")


def check_startup(auth: AuthConfig, host: str, settings: Settings) -> None:
    """Refuse configurations that would break a guarantee before the first request."""
    if auth.mode == "none" and host not in LOOPBACK:
        raise RuntimeError(f"Refusing to listen on {host} without authentication. Set DEVASSIST_COPILOT_AUTH, or use 127.0.0.1.")
    if auth.mode != "none" and not settings.gitlab_projects:
        raise RuntimeError("GITLAB_PROJECTS must list the projects DevAssist may touch when the service is exposed.")


def build_backend(settings: Settings) -> Backend:
    audit = AuditLog(settings.audit_path)
    sessions = SessionStore(settings.audit_path.parent / "copilot_test_sessions.json")
    return Backend(settings, audit, GitLabClient(settings, audit), make_log_store(settings), sessions)


def identify(request: web.Request, auth: AuthConfig) -> Caller | None:
    """Return the caller, or None when the request must be rejected with 401."""
    if auth.api_key and not hmac.compare_digest(request.headers.get(API_KEY_HEADER, ""), auth.api_key):
        return None
    if auth.mode == "easyauth":
        principal = request.headers.get(EASYAUTH_PRINCIPAL, "").strip()
        return Caller(principal.lower()) if principal else None
    return Caller(request.headers.get(REQUESTER_HEADER, "").strip().lower())


def _origin_allowed(request: web.Request, auth: AuthConfig) -> bool:
    """Browsers send Origin; server-to-server calls from Copilot Studio do not. Blocks DNS-rebinding attacks."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    if origin in auth.allowed_origins:
        return True
    return auth.mode == "none" and (urlparse(origin).hostname or "") in LOOPBACK


def create_app(backend: Backend, auth: AuthConfig) -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app[BACKEND_KEY] = backend

    async def caller_or_401(request: web.Request) -> Caller:
        caller = identify(request, auth)
        if caller is None:
            backend.audit.event("auth_rejected", path=request.path, mode=auth.mode)
            raise web.HTTPUnauthorized(text='{"error": "unauthorized"}', content_type="application/json")
        return caller

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def rest_spec(request: web.Request) -> web.Response:
        return web.json_response(build_rest_spec(request.host))

    async def mcp_spec(request: web.Request) -> web.Response:
        return web.json_response(build_mcp_spec(request.host))

    async def rest_tool(request: web.Request) -> web.Response:
        caller = await caller_or_401(request)
        name = request.match_info["name"]
        if name not in TOOLS_BY_NAME:
            return web.json_response({"error": f"Unknown tool {name}"}, status=404)
        try:
            arguments = await request.json() if request.can_read_body else {}
        except ValueError:
            return web.json_response({"error": "The body must be JSON."}, status=400)
        try:
            return web.json_response(await backend.call(name, arguments, caller))
        except ToolError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def mcp_post(request: web.Request) -> web.Response:
        if not _origin_allowed(request, auth):
            return web.json_response({"error": "origin not allowed"}, status=403)
        caller = await caller_or_401(request)
        reply = await handle_message(backend, await request.read(), caller)
        if reply is None:
            return web.Response(status=202)
        return web.json_response(reply)

    async def mcp_other(_request: web.Request) -> web.Response:
        # No server-to-client stream: every reply comes back on its POST.
        return web.Response(status=405, headers={"Allow": "POST"})

    async def close(_app: web.Application) -> None:
        await backend.gitlab.aclose()

    app.router.add_get("/healthz", health)
    app.router.add_get("/openapi.json", rest_spec)
    app.router.add_get("/openapi-mcp.json", mcp_spec)
    app.router.add_post("/api/tools/{name}", rest_tool)
    app.router.add_post("/mcp", mcp_post)
    app.router.add_get("/mcp", mcp_other)
    app.router.add_delete("/mcp", mcp_other)
    app.on_cleanup.append(close)
    return app


async def serve(settings: Settings, auth: AuthConfig, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    import asyncio

    check_startup(auth, host, settings)
    runner = web.AppRunner(create_app(build_backend(settings), auth))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print(f"DevAssist for Copilot Studio on http://{host}:{port}  (MCP: /mcp, REST: /api/tools/<Tool>, auth: {auth.mode})", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
