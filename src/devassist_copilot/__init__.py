"""DevAssist as a Microsoft Copilot Studio agent.

Copilot Studio supplies the model, the conversation and the Teams / Microsoft 365
Copilot channels. This package is the backend it calls: the GitLab, test-loop,
docs and log tools, exposed as an MCP server (for the agent) and as a REST API
described by an OpenAPI 2.0 file (for a Power Platform custom connector and the
agent flow). It reuses the guarded code in ``devassist``: the allow-listed GitLab
client, the PII masking and the audit log.
"""

__version__ = "0.1.0"
