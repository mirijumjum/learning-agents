# DevAssist for Microsoft 365 Copilot (declarative agent)

DevAssist as a **declarative agent**: Microsoft 365 Copilot's orchestrator and model do the reasoning,
and an **API plugin** (the "DevAssist actions API") gives it GitLab, CI and log tools. There are no model
deployments of our own. The full explanation (coverage, licences, costs, deployment) is in Spanish in
[docs/ALTERNATIVA-m365-copilot.md](../docs/ALTERNATIVA-m365-copilot.md).

```
m365-copilot/
  m365agents.yml                Agents Toolkit project: Entra app, SSO auth config, Teams app, extend to M365
  aad.manifest.json             Entra app that protects the API (scope DevAssist.Use)
  env/.env.dev, env/.env.local  values for ${{...}} placeholders (secrets go in .env.*.user, git-ignored)
  appPackage/
    manifest.json               Microsoft 365 app manifest 1.25 with copilotAgents.declarativeAgents
    declarativeAgent.json       declarative agent v1.9 (instructions in instruction.txt)
    instruction.txt             the four specialists: tests, docs, Git, traces
    ai-plugin.json              plugin manifest v2.4, OpenApi runtime, OAuthPluginVault (Entra SSO)
    apiSpecificationFile/devassist-actions.yml   OpenAPI 3.0.1, 18 operations
  src/devassist_actions/        the actions API (reuses src/devassist: GitLab client, guards, masking, logs)
  Dockerfile                    image for the API
```

Run locally against the GitLab stand-in (port 8932) and the API on port 3982:

```bash
devassist fake-gitlab --port 8932
```

```bash
set GITLAB_URL=http://127.0.0.1:8932
set GITLAB_PROJECTS=acme/payments-api,acme/customer-portal
devassist-actions serve --port 3982
```

Validate the manifests against Microsoft's official schemas (downloaded to `.schemas/`) and build the zip:

```bash
devassist-actions validate --env local
```

```bash
devassist-actions package --env local
```
