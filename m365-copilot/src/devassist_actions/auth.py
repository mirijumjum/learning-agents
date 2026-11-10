"""Who is calling: validates the Microsoft Entra access token Copilot sends.

Microsoft 365 Copilot signs the user in (Entra SSO or OAuth authorization code
with PKCE, configured in the plugin's auth config) and calls this API with a
delegated access token in the Authorization header. This module checks that
token: signature against the tenant's published keys, issuer, tenant, audience
and scope. GitLab is still called with the bot account; the user's identity is
used for "assigned to me", ownership of test runs and the audit trail.

With no tenant configured the API runs anonymously, and then it refuses to
listen on anything but loopback (see check_bind_is_safe).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import jwt

_LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Client ID of the Microsoft Enterprise token store that requests tokens for Entra SSO plugins.
ENTERPRISE_TOKEN_STORE_CLIENT_ID = "ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b"


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Caller:
    object_id: str
    name: str
    email: str
    tenant_id: str = ""


ANONYMOUS = Caller(object_id="local", name="local developer", email="")


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


@dataclass(frozen=True)
class AuthConfig:
    tenant_id: str = ""
    audiences: tuple[str, ...] = ()
    required_scope: str = "DevAssist.Use"
    allowed_client_ids: tuple[str, ...] = ()  # empty: any client the tenant issued a token to
    leeway_seconds: int = 60

    @property
    def enabled(self) -> bool:
        return bool(self.tenant_id)

    @property
    def issuers(self) -> tuple[str, ...]:
        return (f"https://login.microsoftonline.com/{self.tenant_id}/v2.0", f"https://sts.windows.net/{self.tenant_id}/")

    @property
    def jwks_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AuthConfig":
        e = os.environ if env is None else env
        config = cls(
            tenant_id=e.get("DEVASSIST_ACTIONS_TENANT_ID", ""),
            audiences=_csv(e.get("DEVASSIST_ACTIONS_AUDIENCES")),
            required_scope=e.get("DEVASSIST_ACTIONS_SCOPE") or "DevAssist.Use",
            allowed_client_ids=_csv(e.get("DEVASSIST_ACTIONS_ALLOWED_CLIENT_IDS")),
        )
        if config.enabled and not config.audiences:
            raise ValueError("DEVASSIST_ACTIONS_TENANT_ID is set, so DEVASSIST_ACTIONS_AUDIENCES must list the API's audiences (api://<client id>, the SSO Application ID URI...).")
        return config


KeyResolver = Callable[[str], Awaitable[Any]]


def jwks_key_resolver(config: AuthConfig) -> KeyResolver:
    """Signing keys from the tenant's JWKS endpoint, cached by PyJWT."""
    client = jwt.PyJWKClient(config.jwks_url, cache_keys=True, lifespan=3600)

    async def resolve(token: str) -> Any:
        return (await asyncio.to_thread(client.get_signing_key_from_jwt, token)).key

    return resolve


@dataclass
class Authenticator:
    config: AuthConfig
    resolve_key: KeyResolver | None = None
    _resolver: KeyResolver | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.config.enabled:
            self._resolver = self.resolve_key or jwks_key_resolver(self.config)

    async def authenticate(self, authorization: str | None) -> Caller:
        if not self.config.enabled:
            return ANONYMOUS
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError(401, "Missing bearer token.")
        token = authorization.split(" ", 1)[1].strip()
        try:
            key = await self._resolver(token)  # type: ignore[misc]
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=list(self.config.audiences),
                leeway=self.config.leeway_seconds, options={"require": ["exp", "iss", "aud", "tid"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(401, f"Invalid token: {exc}") from exc
        except Exception as exc:  # key download failures and the like
            raise AuthError(401, f"Could not validate the token: {type(exc).__name__}") from exc

        if claims.get("iss") not in self.config.issuers or claims.get("tid") != self.config.tenant_id:
            raise AuthError(403, "DevAssist is only available to members of this organisation.")
        scopes = str(claims.get("scp", "")).split()
        if self.config.required_scope not in scopes:
            raise AuthError(403, f"The token lacks the {self.config.required_scope} scope.")
        client = claims.get("azp") or claims.get("appid") or ""
        if self.config.allowed_client_ids and client not in self.config.allowed_client_ids:
            raise AuthError(403, "This client application may not call DevAssist.")
        return Caller(
            object_id=str(claims.get("oid") or claims.get("sub")),
            name=str(claims.get("name") or claims.get("preferred_username") or "unknown"),
            email=str(claims.get("preferred_username") or claims.get("upn") or claims.get("email") or ""),
            tenant_id=str(claims["tid"]),
        )


def check_bind_is_safe(host: str, config: AuthConfig) -> None:
    """Without token validation anyone reaching the port could act as the GitLab bot."""
    if not config.enabled and host not in _LOOPBACK:
        raise RuntimeError(f"Refusing to listen on {host} without authentication. Set DEVASSIST_ACTIONS_TENANT_ID and DEVASSIST_ACTIONS_AUDIENCES, or use 127.0.0.1.")
