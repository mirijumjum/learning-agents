# Container image for agent-lab.
# Used to run the CLI in the cloud (e.g. as an Azure Container Apps Job), see README "Deploying to Azure".
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Never run LLM-generated code as root.
RUN useradd --create-home agent
USER agent
WORKDIR /home/agent

ENTRYPOINT ["agent-lab"]
# Default: the offline demo, handy as a smoke test. Override the arguments to use a real provider.
CMD ["run", "--verbose"]
