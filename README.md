# Multiplatform Automated Agent Orchestrator

A centralized control plane that manages, routes work to and monitors autonomous
AI agents running across **Azure Virtual Machines** and **network-connected PCs**.

Instead of talking to individual agents, users submit tasks to the orchestrator.
It analyses the request, selects the best-suited agent, dispatches the work,
tracks execution telemetry and aggregates results into a single view.

```
                    ┌──────────────────────────────┐
   operators ─────► │      Orchestrator API        │ ◄──── dashboard
   (API key)        │  routing · leases · audit    │
                    └───────┬──────────────┬───────┘
         HMAC-signed control│              │model catalog sync (Entra ID)
              ┌─────────────┼──────────┐   ▼
              ▼             ▼          ▼   ┌──────────────────────┐
      Scout agent    Clawdbot agent  Built-in│  Microsoft Foundry  │
      (Azure VM)     (network PC)  (container)│  model deployments │
            │              │          │      └──────────┬─────────┘
            └──────────────┴──────────┴─ inference ──────┘
                       (each node uses its own managed identity)
```

## Features

| Capability | Where |
| --- | --- |
| Fleet registration, heartbeats, liveness | [orchestrator/registry.py](orchestrator/registry.py) |
| Capability + label based routing engine | [orchestrator/routing.py](orchestrator/routing.py) |
| Microsoft Foundry model catalog sync | [orchestrator/foundry.py](orchestrator/foundry.py) |
| Lease-based dispatch, retries, timeouts | [orchestrator/scheduler.py](orchestrator/scheduler.py) |
| Multi-agent workflow DAGs | [orchestrator/workflows.py](orchestrator/workflows.py) |
| Real-time telemetry per task | [orchestrator/tasks.py](orchestrator/tasks.py) |
| Audit log and governance trail | [orchestrator/audit.py](orchestrator/audit.py) |
| Fleet metrics, token accounting | [orchestrator/metrics.py](orchestrator/metrics.py) |
| Monitoring dashboard | [orchestrator/static/index.html](orchestrator/static/index.html) |
| Agent runtime for VMs / PCs | [agent_node/runner.py](agent_node/runner.py) |
| Scout / Clawdbot / built-in adapters | [agent_node/adapters](agent_node/adapters) |
| Foundry inference on the node | [agent_node/foundry.py](agent_node/foundry.py) |

## Quick start

```powershell
# Create the environment OUTSIDE any synced folder. A venv inside OneDrive or a
# synced SharePoint library uploads certifi's cacert.pem, which trips DLP
# certificate scans (it is only a public CA bundle, but it still gets flagged).
python -m venv C:\venvs\hecaton
C:\venvs\hecaton\Scripts\Activate.ps1
pip install -r requirements.txt

# 1. Control plane
$env:ORCH_API_KEYS   = "demo-operator-key"
$env:ORCH_BOOTSTRAP_TOKEN = "demo-bootstrap-token"
# Model catalog. Use a real project endpoint, or the sample file for an offline demo:
$env:ORCH_FOUNDRY_CATALOG_FILE = "foundry-catalog.example.yaml"
python -m uvicorn orchestrator.main:app --port 8000
```

```powershell
# 2. Agent on an Azure VM (new terminal)
C:\venvs\hecaton\Scripts\Activate.ps1
$env:AGENT_BOOTSTRAP_TOKEN = "demo-bootstrap-token"
python -m agent_node.runner --config agent_node/examples/scout-vm.yaml

# 3. Agent on a network PC (new terminal)
C:\venvs\hecaton\Scripts\Activate.ps1
$env:AGENT_BOOTSTRAP_TOKEN = "demo-bootstrap-token"
python -m agent_node.runner --config agent_node/examples/clawdbot-pc.yaml
```

```powershell
# 4. Run a five-step, two-machine, model-backed workflow
python -m tools.demo --api-key demo-operator-key
```

* Dashboard: <http://localhost:8000/dashboard/> (paste the API key to connect)
* OpenAPI docs: <http://localhost:8000/docs>

Copy [.env.example](.env.example) to `.env` to configure the service permanently.

## Models live in Microsoft Foundry

Agents do not own model configuration. Foundry is the source of truth, and the
orchestrator keeps a mirror of its deployments for routing.

```powershell
pip install -r requirements-foundry.txt
az login   # or assign a managed identity to the VM
$env:FOUNDRY_PROJECT_ENDPOINT = "https://<account>.services.ai.azure.com/api/projects/<project>"
```

What happens then:

1. **Sync** — on startup, every `ORCH_FOUNDRY_SYNC_INTERVAL` seconds, and on
   demand via `POST /api/v1/models/sync`, the control plane enumerates the
   project's deployments with `azure-ai-projects` and upserts them into the
   `model_deployments` table. Deployments removed in Foundry are marked
   `unavailable` rather than deleted, so historical tasks stay explainable.
2. **Normalise** — Foundry's capability flags (`chatCompletion`, `assistants`,
   `jsonObjectResponse`, ...) plus model-family hints are mapped onto one
   vocabulary: `chat`, `reasoning`, `tool-calling`, `json-mode`, `vision`,
   `audio`, `embeddings`, `image-generation`, `fine-tuning`.
3. **Route** — a submission either pins a deployment (`requested_model`) or
   states what it needs (`required_model_capabilities`). The orchestrator picks
   the least over-provisioned available deployment that satisfies it, breaking
   ties with `ORCH_MODEL_PREFERENCE`. Only agents advertising
   `foundry.inference` are eligible for model-bound work.
4. **Dispatch** — the lease response carries a `model` binding: deployment name,
   model name, version, endpoint and project. **No key or token is included.**
   The node authenticates to Foundry itself with `DefaultAzureCredential`.
5. **Account** — agents return `usage` on completion; tokens are stored per task
   and aggregated per deployment in `GET /api/v1/metrics/summary` and on the
   dashboard.

If a task needs a model that Foundry does not currently publish, it stays
`pending` with a `model_pending` event explaining why, and starts automatically
once a matching deployment appears.

Without `azure-ai-projects` installed, both the catalog (via
`ORCH_FOUNDRY_CATALOG_FILE`) and inference fall back to simulation so the demo
still runs end to end.

## How routing works

1. A submission declares an `action` (`research.web`, `shell.exec`, ...), optional
   `required_capabilities`, a `label_selector`, a preferred framework and a priority.
2. If capabilities are not declared they are **inferred** from the action namespace
   (`research.*` → `research` + `browser.automation`, `shell.*` → `shell.exec`, ...).
3. Hard filters remove agents that are offline, saturated, missing a capability or
   failing the label selector.
4. Survivors are scored on framework affinity, spare capacity, capability
   specificity, label overlap and heartbeat freshness. The highest score wins.
5. If the task needs a model, a Foundry deployment is chosen in the same pass and
   bound to the assignment.

`POST /api/v1/tasks/preview-routing` dry-runs this engine and explains both the
agent and the model choice (or the exact capability gap) without queueing
anything — the dashboard's **Preview routing** button.

## Task lifecycle

```
pending ──lease──► assigned ──progress──► running ──┬─► succeeded
   ▲                   │                            ├─► failed  (after max_attempts)
   └── retry / lease expiry ────────────────────────┴─► timed_out
```

Agents pull work with a long-poll lease (`POST /api/v1/agents/{id}/lease`), which
works through NAT and corporate firewalls without inbound ports on the node. A
background reaper re-queues tasks whose lease expired and fails tasks that blow
their execution timeout.

## Workflows

A workflow is a validated DAG of steps; cycles and unknown dependencies are
rejected at submission time. Steps become tasks, each routed independently — so a
single workflow naturally spans multiple machines and frameworks. When every
dependency of a step succeeds it is unblocked; if any fails, dependents are
cancelled and the workflow is marked failed with a consolidated result document.

## Security model

| Boundary | Control |
| --- | --- |
| Operator → orchestrator | `X-API-Key` (swap for Entra ID bearer tokens in production — single dependency in [orchestrator/security.py](orchestrator/security.py)) |
| Agent enrolment | One-time `X-Bootstrap-Token`, server issues a per-agent secret |
| Agent → orchestrator | HMAC-SHA256 over version, method, path, timestamp, nonce and body hash |
| Orchestrator → Foundry | Entra ID via `DefaultAzureCredential`, no model keys stored |
| Agent → Foundry | The node's own managed identity; the assignment carries only deployment coordinates |
| Replay protection | Timestamp skew window + single-use nonce cache |
| Impersonation | Path agent id must match the signing identity (403 otherwise) |
| Task ownership | Progress/completion accepted only from the agent holding the lease |
| Agent file access | Sandboxed to a workspace root, symlink/`..` escapes rejected |
| Agent shell access | Deny-by-default allow-list, `shell=False`, no shell interpolation |
| Agent HTTP access | http/https only, loopback / link-local / metadata endpoints blocked |
| Data access | Every SQL statement is parameterised |
| Dashboard | Rendered via DOM APIs only, never `innerHTML` |
| Governance | Append-only audit log of every registration, submission and completion |

Agent secrets are currently stored in the orchestrator database. For a production
deployment put them in **Azure Key Vault** and give the control plane a managed
identity; the only touch point is the `agents.secret` column.

## API surface

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/agents/register` | bootstrap | Enrol an agent, receive its signing secret |
| POST | `/api/v1/agents/{id}/heartbeat` | agent | Liveness + lease renewal |
| POST | `/api/v1/agents/{id}/lease` | agent | Long-poll for assigned work |
| POST | `/api/v1/tasks/{id}/progress` | agent | Stream telemetry |
| POST | `/api/v1/tasks/{id}/complete` | agent | Report the terminal result |
| GET | `/api/v1/agents` | operator | Fleet inventory |
| POST | `/api/v1/agents/{id}/drain` | operator | Stop scheduling new work to a node |
| DELETE | `/api/v1/agents/{id}` | operator | Deregister a node |
| POST | `/api/v1/tasks` | operator | Submit a task |
| POST | `/api/v1/tasks/preview-routing` | operator | Explain the routing decision |
| GET | `/api/v1/tasks` · `/{id}` · `/{id}/events` | operator | Status and telemetry |
| POST | `/api/v1/tasks/{id}/cancel` | operator | Cancel a task |
| POST | `/api/v1/workflows` | operator | Submit a multi-agent DAG |
| GET | `/api/v1/workflows` · `/{id}` | operator | Workflow status + aggregated result |
| POST | `/api/v1/workflows/{id}/cancel` | operator | Cancel a workflow |
| GET | `/api/v1/models` · `/{name}` | operator | Foundry deployment catalog |
| POST | `/api/v1/models/sync` | operator | Force a Foundry re-sync |
| GET | `/api/v1/audit` | operator | Governance trail |
| GET | `/api/v1/metrics/summary` | operator | Fleet KPIs and token usage |

## Connecting real agents

Adapters run in **simulation mode** until you point them at a framework endpoint,
so the whole system is demoable without Scout or Clawdbot installed. To go live,
set the endpoint in the agent config:

```yaml
frameworks:
  scout:
    enabled: true
    endpoint: http://127.0.0.1:7801   # must expose POST /run
```

The bridge posts `{task_id, action, title, input}` to `{endpoint}/run` and treats
the JSON response as the task result. Implement a different protocol by adding a
class to [agent_node/adapters](agent_node/adapters) that satisfies the `Adapter`
protocol — `handles()`, `capabilities()` and `execute()`.

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

Covers authentication, signature tampering, nonce replay, impersonation, routing
decisions, the full task lifecycle, double-lease prevention, retry semantics,
concurrency limits, workflow dependency handling, audit/metrics output, plus the
Foundry catalog sync, capability normalisation, deployment retirement,
model-aware routing, keyless bindings and token accounting.

## Scaling notes

The hackathon build uses embedded SQLite and a single process. The seams for
scale-out are deliberate:

* `orchestrator/db.py` — swap for Azure SQL / PostgreSQL; the guarded
  `UPDATE ... WHERE status = 'pending'` claim already makes leasing safe for
  multiple orchestrator replicas.
* `orchestrator/scheduler.py` — the reaper is idempotent and can run in every replica.
* Long-poll leasing means agents scale to hundreds of nodes without inbound ports.
