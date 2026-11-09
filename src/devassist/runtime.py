"""Builds a ready-to-use DevAssist from settings."""

from __future__ import annotations

from devassist.audit import AuditLog
from devassist.config import Settings
from devassist.gitlab import GitLabClient
from devassist.llm import LLM
from devassist.logstore import make_log_store
from devassist.orchestrator import DevAssist


def build_devassist(settings: Settings) -> DevAssist:
    audit = AuditLog(settings.audit_path)
    return DevAssist(settings, audit, LLM(settings, audit), GitLabClient(settings, audit), make_log_store(settings))
