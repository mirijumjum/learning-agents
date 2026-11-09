"""Builds the Teams / Microsoft 365 Copilot app package (a zip you upload)."""

from __future__ import annotations

import json
import re
import uuid
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any

MANIFEST_VERSION = "1.25"
SCHEMA_URL = f"https://developer.microsoft.com/json-schemas/teams/v{MANIFEST_VERSION}/MicrosoftTeams.schema.json"
_GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_APP_NAMESPACE = uuid.UUID("7f1f6c5e-2b1f-4f0e-9a44-5b1d3f6a0c11")


def build_manifest(bot_id: str, developer: str, website: str, privacy_url: str, terms_url: str, version: str = "1.0.0") -> dict[str, Any]:
    if not _GUID.match(bot_id):
        raise ValueError("--bot-id must be the GUID of the bot's Microsoft Entra application or managed identity.")
    for url in (website, privacy_url, terms_url):
        if not url.startswith("https://"):
            raise ValueError(f"Teams requires https:// URLs, got {url!r}.")
    return {
        "$schema": SCHEMA_URL,
        "manifestVersion": MANIFEST_VERSION,
        "version": version,
        "id": str(uuid.uuid5(_APP_NAMESPACE, bot_id)),  # stable, so an update replaces the same app
        "developer": {"name": developer, "websiteUrl": website, "privacyUrl": privacy_url, "termsOfUseUrl": terms_url},
        "name": {"short": "DevAssist", "full": "DevAssist development agents"},
        "description": {
            "short": "Tests, docs, GitLab and root-cause analysis from Teams.",
            "full": "DevAssist generates and runs unit tests, writes technical documentation, works with GitLab branches and merge requests, and finds the root cause of failures. It never merges, deploys or deletes.",
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#2F6FED",
        "bots": [{
            "botId": bot_id,
            "scopes": ["personal", "team", "groupChat"],
            "isNotificationOnly": False,
            "supportsFiles": False,
            "commandLists": [{"scopes": ["personal", "team", "groupChat"], "commands": [
                {"title": "help", "description": "What DevAssist can do"},
                {"title": "feedback good", "description": "Tell us the last answer helped"},
                {"title": "feedback bad", "description": "Tell us what went wrong"},
            ]}],
        }],
        "copilotAgents": {"customEngineAgents": [{"type": "bot", "id": bot_id}]},
        "webApplicationInfo": {"id": bot_id, "resource": f"api://botid-{bot_id}"},
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": [],
    }


def write_package(manifest: dict[str, Any], output: Path) -> Path:
    icons = resources.files("devassist").joinpath("app_package")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name in ("color.png", "outline.png"):
            archive.writestr(name, icons.joinpath(name).read_bytes())
    return output
