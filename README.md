# learning-agents · `agent-lab`

A tiny CLI to learn **[Microsoft Agent Framework](https://github.com/microsoft/agent-framework)** (Python)
by building something concrete: three AI agents in a workflow that **write a function, write tests for it,
and run them**, looping back with feedback until the tests pass.

```
            FeatureRequest
                 │
                 ▼
        ┌──────────────┐  CodeDraft   ┌──────────────┐  GeneratedTests  ┌──────────────┐
  ┌───▶ │ 🧑‍💻 coder     │ ───────────▶ │ 🧪 tester     │ ───────────────▶ │ 🏃 runner     │
  │     │  (LLM agent) │              │  (LLM agent) │                  │ (agent+tool) │
  │     └──────────────┘              └──────────────┘                  └──────┬───────┘
  │                                                                       RunReport
  │                                                                            ▼
  │        FeatureRequest (attempt+1, feedback)                     ┌──────────────────┐
  └──────────────────────────────────────────────────────────────── │ 🚦 quality_gate   │ ──▶ FinalResult
                                                                     │  (plain Python)  │    (workflow output)
                                                                     └──────────────────┘
```

1. **coder** (agent) writes `solution.py`.
2. **tester** (agent) writes pytest tests. It only sees the description and the function *signatures*, never the
   implementation, so it can't copy the coder's bugs.
3. **runner** (agent with a `run_pytest` tool) runs the tests and summarises the result.
4. **quality_gate** (plain Python, no LLM) finishes if the tests pass, or sends the failure back to the coder
   (up to `--max-attempts`, default 3).

## Table of contents

- [Quick start](#quick-start)
- [Why Microsoft Agent Framework?](#why-microsoft-agent-framework)
- [Do I need Azure? Why is Azure OpenAI supported?](#do-i-need-azure-why-is-azure-openai-supported)
- [Configuration: environment variables](#configuration-environment-variables)
- [Concepts, mapped to the code](#concepts-mapped-to-the-code)
- [Project layout](#project-layout)
- [Deploying to Azure (step by step)](#deploying-to-azure-step-by-step)
- [Next steps: making it accessible to other people](#next-steps-making-it-accessible-to-other-people)
- [Ideas to keep learning](#ideas-to-keep-learning)

## Quick start

Requirements: Python 3.10+.

```bash
git clone https://github.com/mirijumjum/learning-agents.git
cd learning-agents
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # add '.[anthropic]' for Anthropic support
agent-lab run -v            # offline demo, no API key needed
```

The default `mock` provider is an offline, scripted LLM. In the palindrome demo its coder makes a
deliberate bug on the first attempt, so you can watch the tests fail, the feedback loop kick in and
the second attempt pass.

```bash
agent-lab run "fizzbuzz"                    # passes on the first try
agent-lab run "palindrome" -n 1             # only 1 attempt -> ends in failure (exit code 1)
agent-lab run "palindrome" --trace          # also print every raw workflow event
agent-lab graph                             # Mermaid diagram of the workflow (or: -f dot)
agent-lab providers                         # list providers and their env vars
python -m pytest                            # the project's own test suite
```

With a real LLM you can ask for anything:

```bash
agent-lab run "A function that parses durations like '1h30m' into seconds" --provider azure -v
```

> ⚠️ The runner executes LLM-generated code on your machine (60 s timeout, inside `./workspace`).
> That's fine for learning, but it is **not** a sandbox. See [hardening](#hardening-checklist) before exposing it.

## Why Microsoft Agent Framework?

The goal of this project is to learn **agents** and **workflows**. Microsoft Agent Framework fits that well:

- **It covers both ideas in one library.** An *agent* is an LLM that decides what to do (e.g. whether to call
  a tool). A *workflow* is a graph **you** define, where each node can be an agent or ordinary code. Many
  libraries are good at one of the two; this one is designed around both.
- **It is the successor of Semantic Kernel and AutoGen.** Microsoft merged its two agent projects into this
  one, so it's where their effort goes now, with versions for both **Python and .NET**.
- **Typed, validated graphs.** Executors declare the message types they accept and send; `build()` rejects
  edges between nodes that don't speak the same type. Cycles (our retry loop), fan-out/fan-in, conditional
  edges, checkpoints and human-in-the-loop all come built in.
- **Provider-agnostic.** Agents talk to a *chat client* interface. Swapping OpenAI, Azure OpenAI, Anthropic, a
  local Ollama or our offline mock changes **one file** (`providers.py`), not the workflow.
- **Production features you can grow into:** streaming events, middleware, OpenTelemetry tracing, and
  first-class hosting options on Azure (Functions, Foundry), which matters for the deployment steps below.

## Do I need Azure? Why is Azure OpenAI supported?

**No, Azure is not required.** The project runs fully offline with `--provider mock`, and also works with
OpenAI or Anthropic directly. Azure OpenAI is offered because it's the natural choice once you go beyond
learning with a Microsoft framework:

| Reason | What it means |
|---|---|
| **Same models, your subscription** | Azure OpenAI serves OpenAI models from *your* Azure resource, billed to your subscription. |
| **Data stays in your tenant and region** | You pick the region; prompts and outputs aren't used to train models. Important for company data. |
| **Enterprise authentication** | Besides API keys it supports Microsoft Entra ID and *managed identities*, so cloud services authenticate with no secrets at all. |
| **Networking and governance** | Private endpoints, quotas, content filtering, and Azure Policy/RBAC like any other Azure resource. |
| **Easy deployment** | If the model is in Azure, running the app in Azure (Container Apps, Functions…) next to it is the shortest path. |

**Why it's implemented this way:** Azure OpenAI exposes the same API as OpenAI, so both providers use the same
class, `OpenAIChatCompletionClient`. The only difference is that for Azure we pass `azure_endpoint`. We use the
**Chat Completions** API (instead of the newer Responses API) because almost every OpenAI-compatible server
supports it, including Ollama and other local servers. To start we authenticate with an **API key** because it's
the simplest option; switching to managed identity is listed in the [hardening checklist](#hardening-checklist).

## Configuration: environment variables

Pick **one** provider with `--provider` and set its variables. The easiest way is a `.env` file:

```bash
cp .env.example .env        # then edit .env with your values
set -a; source .env; set +a # load it into the current shell
```

`.env` is in `.gitignore`: never commit real keys.

### `--provider mock` (default)

No variables. It only has scripts for three demos: palindrome, fizzbuzz and celsius.

### `--provider azure` (Azure OpenAI)

| Variable | Required | Example | Where to find it |
|---|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | ✅ | `https://my-resource.openai.azure.com/` | Azure portal → your resource → *Keys and Endpoint* |
| `AZURE_OPENAI_API_KEY` | ✅ | `3f9a…` | Same page, *KEY 1* |
| `AZURE_OPENAI_MODEL` | ✅ (or `--model`) | `agent-lab-model` | The **deployment name** you chose, not the model name |
| `AZURE_OPENAI_API_VERSION` | optional | `2024-10-21` | Only if you need a specific API version |

### `--provider openai` (OpenAI or any compatible server)

| Variable | Required | Example |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | `sk-…` (any non-empty value for local servers like Ollama) |
| `OPENAI_MODEL` | ✅ (or `--model`) | a model name your account or server offers |
| `OPENAI_BASE_URL` | optional | `http://localhost:11434/v1` for a local Ollama |

### `--provider anthropic`

Needs `pip install -e '.[anthropic]'`.

| Variable | Required | Example |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | `sk-ant-…` |
| `ANTHROPIC_CHAT_MODEL` | ✅ (or `--model`) | a model name from the Anthropic docs |

### Other

| Variable | Effect |
|---|---|
| `NO_COLOR` | Any value disables colored output (useful in cloud logs). |

If something is missing, the CLI says exactly which variable and exits with code `2`.

## Concepts, mapped to the code

| Concept | What it is | Where |
|---|---|---|
| **Chat client** | The connection to an LLM. The rest of the framework only needs "messages in, response out". | `providers.py`, `mock_client.py` |
| **Agent** | Chat client + instructions (persona) + optional tools. `await agent.run(prompt)` runs one full turn. | `agents.py` |
| **Tool** | A Python function the model may call. Name, docstring and type hints become a JSON schema. | `tools.py` (`run_pytest`) |
| **Function-calling loop** | Model asks for a tool → framework runs it → result goes back to the model. Provided by `FunctionInvocationLayer`. | `mock_client.py` shows it explicitly |
| **Executor** | A node of the workflow. `@handler` methods; the input type decides which messages it receives. | `executors.py` |
| **WorkflowContext** | How a handler talks to the graph: `send_message()` to the next node, `yield_output()` for the final result. | `executors.py` |
| **Edges / graph** | `WorkflowBuilder(...).add_chain([...]).add_edge(gate, coder).build()`; `build()` validates types and connectivity. | `workflow.py` |
| **Cycles** | The `quality_gate → coder` edge turns a pipeline into a "write, test, fix" loop. | `workflow.py` |
| **Events / streaming** | `workflow.run(msg, stream=True)` yields events (`executor_invoked`, `executor_completed`, `output`, …). | `cli.py` (`EventPrinter`) |
| **Supersteps** | Execution happens in rounds: every message sent in one round is delivered in the next. | visible with `--trace` |

### Design choices worth noticing

- **Agents vs. plain code.** Three nodes use an LLM, the quality gate doesn't. Use AI only where judgement is needed.
- **Don't trust the LLM with measurable facts.** The runner agent calls pytest through a tool, but the *real*
  exit code is recorded by `PytestRunner`, and that's what the gate uses. If the agent never calls the tool,
  the executor runs pytest itself.
- **Black-box testing.** The tester only sees the spec plus the *signatures and docstrings* of the code
  (`describe_public_api`), not the implementation.
- **Typed messages.** Dataclasses (`messages.py`) instead of strings make the graph self-documenting.
- **Every attempt is kept** in `workspace/<run>/attempt_N/`, so you can diff what changed between attempts.

## Project layout

```
src/agent_lab/
  cli.py          argparse CLI + event printer
  workflow.py     builds the graph
  executors.py    coder / tester / runner / quality_gate nodes
  agents.py       agent instructions and factories
  tools.py        run_pytest tool
  messages.py     dataclasses that flow along the edges
  providers.py    mock / openai / azure / anthropic clients
  mock_client.py  offline scripted chat client
  code_utils.py   extract code from LLM answers, summarise a module's API
tests/            pytest suite for the project itself
Dockerfile        container image (used for the Azure deployment)
.env.example      template with every environment variable
```

---

## Deploying to Azure (step by step)

**Goal:** run `agent-lab` in the cloud against Azure OpenAI, without needing your laptop.

**Architecture:** the CLI is a program that starts, does its work and exits, so the right Azure service is a
**Container Apps Job**: a container that runs on demand and costs nothing while idle.

```
 your terminal ──(az CLI)──▶ Azure Container Registry ──image──▶ Container Apps Job ──▶ Azure OpenAI
                                                                        │
                                                                        └──▶ logs (Log Analytics)
```

### Step 0 · Prerequisites

1. An Azure subscription. A free account at https://azure.microsoft.com/free works.
2. The Azure CLI: https://learn.microsoft.com/cli/azure/install-azure-cli
3. Sign in and pick the subscription:

   ```bash
   az login
   az account show --query "{name:name, id:id}" -o table
   ```

You do **not** need Docker locally: the image is built in the cloud by `az acr build`.

### Step 1 · Choose names (copy, edit and paste)

Some names must be globally unique and lowercase, so add your initials or a number.

```bash
export RG=rg-agent-lab                 # resource group: a "folder" for everything we create
export LOCATION=swedencentral          # a region where Azure OpenAI is available
export AOAI_NAME=aoai-agentlab-mj01    # Azure OpenAI resource (globally unique)
export DEPLOYMENT=agent-lab-model      # name of your model deployment
export ACR_NAME=acragentlabmj01        # container registry (globally unique, letters/numbers only)
export ENV_NAME=cae-agent-lab          # Container Apps environment
export JOB_NAME=agent-lab-job          # the job that runs the CLI
```

### Step 2 · Create the resource group

```bash
az group create --name $RG --location $LOCATION
```

Everything below lives inside it, so cleaning up later is a single command ([Step 9](#step-9--clean-up)).

### Step 3 · Create Azure OpenAI and deploy a model

```bash
# 3.1 The Azure OpenAI resource
az cognitiveservices account create \
  --name $AOAI_NAME --resource-group $RG --location $LOCATION \
  --kind OpenAI --sku S0 --custom-domain $AOAI_NAME

# 3.2 See which models you can deploy in this region (pick a chat model and its version)
az cognitiveservices account list-models \
  --name $AOAI_NAME --resource-group $RG \
  --query "[].{model:name, version:version}" -o table

# 3.3 Deploy it (replace <model-name> and <model-version> with values from the table above)
az cognitiveservices account deployment create \
  --name $AOAI_NAME --resource-group $RG \
  --deployment-name $DEPLOYMENT \
  --model-name <model-name> --model-version <model-version> --model-format OpenAI \
  --sku-name GlobalStandard --sku-capacity 10
```

> Prefer clicking? Do the same in the [Azure AI Foundry portal](https://ai.azure.com): create a resource,
> then *Models + endpoints → Deploy model*. The **deployment name** you type there is your `AZURE_OPENAI_MODEL`.

### Step 4 · Get the endpoint and key, and test locally first

```bash
export AZURE_OPENAI_ENDPOINT=$(az cognitiveservices account show \
  --name $AOAI_NAME --resource-group $RG --query properties.endpoint -o tsv)
export AZURE_OPENAI_API_KEY=$(az cognitiveservices account keys list \
  --name $AOAI_NAME --resource-group $RG --query key1 -o tsv)
export AZURE_OPENAI_MODEL=$DEPLOYMENT

agent-lab run "A function that parses durations like '1h30m' into seconds" --provider azure -v
```

If this works on your machine, the cloud version will too: from here on it's just packaging.

### Step 5 · Build the container image in Azure

```bash
# 5.1 A private registry for your images
az acr create --name $ACR_NAME --resource-group $RG --sku Basic --admin-enabled true

# 5.2 Build the image from this folder (where the Dockerfile is). It runs in Azure, no local Docker needed.
az acr build --registry $ACR_NAME --image agent-lab:v1 .
```

### Step 6 · Create the Container Apps environment

```bash
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights

az containerapp env create --name $ENV_NAME --resource-group $RG --location $LOCATION
```

The environment is the shared "space" where apps and jobs run. It also creates a Log Analytics workspace for logs.

### Step 7 · Create the job

The API key goes in as a **secret** (`--secrets`) and the env var only references it (`secretref:`),
so the key never appears in plain text in the job's configuration.

```bash
az containerapp job create \
  --name $JOB_NAME --resource-group $RG --environment $ENV_NAME \
  --trigger-type Manual --replica-timeout 1800 --replica-retry-limit 0 \
  --replica-completion-count 1 --parallelism 1 \
  --image $ACR_NAME.azurecr.io/agent-lab:v1 \
  --registry-server $ACR_NAME.azurecr.io \
  --registry-username $(az acr credential show --name $ACR_NAME --query username -o tsv) \
  --registry-password $(az acr credential show --name $ACR_NAME --query "passwords[0].value" -o tsv) \
  --cpu 0.5 --memory 1Gi \
  --secrets aoai-key=$AZURE_OPENAI_API_KEY \
  --env-vars AZURE_OPENAI_ENDPOINT=$AZURE_OPENAI_ENDPOINT \
             AZURE_OPENAI_API_KEY=secretref:aoai-key \
             AZURE_OPENAI_MODEL=$DEPLOYMENT \
             NO_COLOR=1 \
  --args run "A function that parses durations like '1h30m' into seconds" --provider azure --verbose
```

`--args` are the arguments passed to `agent-lab` (the image's entrypoint), exactly as you'd type them locally.

### Step 8 · Run it and read the logs

```bash
# Start one execution
az containerapp job start --name $JOB_NAME --resource-group $RG

# See its status (Running / Succeeded / Failed)
az containerapp job execution list --name $JOB_NAME --resource-group $RG -o table
```

To read the output:

- **Portal:** open the job → *Execution history* → *Console logs* of the execution.
- **Query:** in the Log Analytics workspace of the environment, run:

  ```kusto
  ContainerAppConsoleLogs_CL
  | where ContainerJobName_s == "agent-lab-job"
  | project TimeGenerated, Log_s
  | order by TimeGenerated asc
  ```

  Logs can take a couple of minutes to appear.

To ask for a different function, change the arguments and start again:

```bash
az containerapp job update --name $JOB_NAME --resource-group $RG \
  --args run "A function that validates Spanish DNI numbers" --provider azure --verbose
az containerapp job start --name $JOB_NAME --resource-group $RG
```

To ship a new version of the code: `az acr build --registry $ACR_NAME --image agent-lab:v2 .`, then
`az containerapp job update --name $JOB_NAME --resource-group $RG --image $ACR_NAME.azurecr.io/agent-lab:v2`.

### Step 9 · Clean up

Delete everything when you're done, so nothing keeps costing money:

```bash
az group delete --name $RG --yes --no-wait
```

> 💸 **Costs, roughly:** Azure OpenAI charges per token used; the job only while it's running (per second);
> the Basic registry has a small fixed monthly fee. An idle setup costs almost nothing except the registry,
> and Step 9 removes everything.

---

## Next steps: making it accessible to other people

The job above is for **you** (you start it from your terminal). To let **other people or apps** use it,
expose it through an HTTP API. These are the options, from simplest to most "managed":

### Option A · HTTP API on Azure Container Apps (recommended next step)

1. **Add a web layer.** `pip install fastapi uvicorn` and create `src/agent_lab/api.py`:

   ```python
   from pathlib import Path
   from tempfile import mkdtemp

   from fastapi import FastAPI
   from pydantic import BaseModel

   from agent_lab.messages import FeatureRequest
   from agent_lab.providers import make_client_factory
   from agent_lab.workflow import build_workflow

   app = FastAPI(title="agent-lab")


   class RunRequest(BaseModel):
       description: str
       max_attempts: int = 3


   @app.post("/runs")
   async def create_run(body: RunRequest) -> dict:
       workspace = Path(mkdtemp(prefix="agent-lab-"))
       workflow = build_workflow(make_client_factory("azure"), workspace)
       result = await workflow.run(FeatureRequest(description=body.description, max_attempts=body.max_attempts))
       final = result.get_outputs()[0]
       return {
           "success": final.success,
           "attempts": final.attempts,
           "summary": final.summary,
           "code": final.solution_path.read_text(),
           "tests": final.tests_path.read_text(),
       }
   ```

   This reuses the **same workflow**. Only the entry point changes from CLI to HTTP.

2. **Try it locally:** `uvicorn agent_lab.api:app --reload`, then open http://localhost:8000/docs to call
   `POST /runs` from the browser.
3. **Change the image's entrypoint** in the `Dockerfile` (add `fastapi` and `uvicorn` to `pyproject.toml` first):

   ```dockerfile
   EXPOSE 8000
   ENTRYPOINT ["uvicorn", "agent_lab.api:app", "--host", "0.0.0.0", "--port", "8000"]
   CMD []
   ```

4. **Build and deploy it as a Container App** (an always-reachable service, unlike the job):

   ```bash
   az acr build --registry $ACR_NAME --image agent-lab-api:v1 .
   az containerapp create \
     --name agent-lab-api --resource-group $RG --environment $ENV_NAME \
     --image $ACR_NAME.azurecr.io/agent-lab-api:v1 \
     --registry-server $ACR_NAME.azurecr.io \
     --registry-username $(az acr credential show --name $ACR_NAME --query username -o tsv) \
     --registry-password $(az acr credential show --name $ACR_NAME --query "passwords[0].value" -o tsv) \
     --target-port 8000 --ingress external \
     --min-replicas 0 --max-replicas 2 \
     --secrets aoai-key=$AZURE_OPENAI_API_KEY \
     --env-vars AZURE_OPENAI_ENDPOINT=$AZURE_OPENAI_ENDPOINT AZURE_OPENAI_API_KEY=secretref:aoai-key \
                AZURE_OPENAI_MODEL=$DEPLOYMENT \
     --query properties.configuration.ingress.fqdn -o tsv
   ```

   The command prints the public URL (`https://agent-lab-api.<something>.azurecontainerapps.io`).
   `--min-replicas 0` means it scales to zero and costs nothing while nobody uses it.

5. **Protect it before sharing the URL!** A public endpoint that runs generated code and spends your tokens must
   require sign-in. The easiest way is the built-in authentication: portal → the container app →
   *Authentication* → *Add identity provider* → **Microsoft**. Only users of your tenant (or the ones you allow) get in.
6. **Long requests.** A run can take a minute or more. For real use, return `202 Accepted` with a run id right
   away and let the client poll `GET /runs/{id}`, or put requests on a queue (Azure Service Bus) that triggers the
   job from the previous section.

### Option B · Azure Functions (serverless)

Agent Framework has an Azure Functions integration (`agent-framework-azurefunctions`) built on Durable Functions.
It gives you HTTP endpoints, durable state and long runs without managing containers. A good fit if you already
use Functions; the trade-off is learning the Functions programming model.

### Option C · Microsoft Foundry (managed agents)

Azure AI Foundry can host agents for you (`agent-framework-foundry` and `agent-framework-foundry-hosting`),
with a playground, evaluations, tracing and access control in the portal. It's the most managed option, best
when the agents become a product inside your company.

### Option D · DevUI (local demos, not production)

To *show* the workflow to someone from your machine, the framework ships a web UI:

```bash
pip install agent-framework-devui
```

```python
from pathlib import Path
from agent_framework.devui import serve
from agent_lab.providers import make_client_factory
from agent_lab.workflow import build_workflow

serve(
    entities=[build_workflow(make_client_factory("mock"), Path("workspace"))],
    port=8080,
    auth_token="local-demo",  # DevUI's API requires a bearer token; enter this one if the UI asks
    auto_open=True,
)
```

Then open http://localhost:8080, pick the `write-test-run` workflow and watch each executor run.

### Hardening checklist

Before real users touch it:

- [ ] **No keys at all: use managed identity.** Give the Container App a system-assigned identity, grant it the
  *Cognitive Services OpenAI User* role on the Azure OpenAI resource, and build the client with
  `credential=DefaultAzureCredential()` (package `azure-identity`) instead of `api_key`. Then delete
  `AZURE_OPENAI_API_KEY`. Use the same idea for the registry (`--registry-identity system`) and turn off ACR admin.
- [ ] **Secrets in Key Vault** if you still need any, referenced from Container Apps.
- [ ] **Sandbox the generated code.** Today the tests run inside the app's own container. Azure Container Apps
  *dynamic sessions* are isolated sandboxes made to run untrusted, LLM-generated code. Move the `run_pytest` tool there.
- [ ] **Persist results:** save each run's `workspace/` to Azure Blob Storage instead of the container's temporary disk.
- [ ] **Observability:** the framework emits OpenTelemetry traces; send them to Application Insights to see every
  agent call, tool call and token count.
- [ ] **Limits:** cap `max_attempts`, request size and requests per user, so costs stay predictable.
- [ ] **CI/CD:** a GitHub Actions workflow that runs `python -m pytest`, builds the image and updates the job/app on every push to `main`.

## Ideas to keep learning

1. **Fan-out / fan-in:** add a second tester with a different persona (e.g. "edge-case hunter") and merge
   both suites with `add_fan_out_edges` / `add_fan_in_edges`.
2. **Conditional edges:** replace the gate's `if` with `add_switch_case_edge_group`.
3. **Human in the loop:** use `ctx.request_info(...)` so a human approves the code before tests run.
4. **Checkpoints:** pass `checkpoint_storage=FileCheckpointStorage(...)` to `WorkflowBuilder` and resume a run.
5. **Structured output:** make the runner return a Pydantic model (`options={"response_format": ...}`).
6. **Workflow as an agent:** `workflow.as_agent()` exposes the whole graph as a single agent.
