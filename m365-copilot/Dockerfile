# DevAssist actions API for the Microsoft 365 Copilot declarative agent.
# Build from the repository root:  docker build -f m365-copilot/Dockerfile -t devassist-actions .
# No model runs here: Copilot does the reasoning, GitLab runners run the tests.
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY m365-copilot/src ./m365-copilot/src
RUN pip install --no-cache-dir ".[azure]"

RUN useradd --create-home devassist
USER devassist
# Audit trail and test-run state. Mount a volume here so runs survive a new revision.
ENV DEVASSIST_AUDIT_PATH=/home/devassist/data/audit.jsonl

EXPOSE 3982
# Listening on 0.0.0.0 is refused unless DEVASSIST_ACTIONS_TENANT_ID and DEVASSIST_ACTIONS_AUDIENCES are set.
ENTRYPOINT ["devassist-actions", "serve", "--host", "0.0.0.0", "--port", "3982"]
