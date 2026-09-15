# Architecture

## The problem this solves

Running one autonomous agent is easy. Running twenty across different machines is not: you
need to know which machine can do the work, hand it credentials it should not keep, survive
the machine going offline mid-task, and afterwards be able to say exactly what happened.

This system treats a fleet of machines as one addressable workforce. An operator submits a
goal; the control plane decides where it runs and which model backs it.

## Three tiers

```
   operator ──API key──► ┌──────────────────────────┐ ◄── dashboard
                         │     Orchestrator         │
                         │  routing · leases · DAGs │
                         │  audit · metrics         │
                         └────┬──────────────┬──────┘
          HMAC-signed, outbound│              │ Entra ID
              ┌───────────────┼────────┐     ▼
              ▼               ▼        ▼   ┌───────────────────┐
         agent node      agent node   ...  │ Microsoft Foundry │
         (Azure VM)      (network PC)      │ model deployments │
              │               │            └─────────┬─────────┘
              └───────────────┴── inference ─────────┘
                    (each node uses its own managed identity)
```

**Control plane** — FastAPI over SQLite. Owns registration, routing, leasing, workflow
orchestration, audit and metrics. Stateless enough that the storage layer is the only thing
standing between it and horizontal scale-out.

**Agent nodes** — a Python runtime per machine. Nodes expose no inbound port. They acquire
work by long-polling a lease endpoint, which is why the same binary works on an Azure VM
with no public IP and on a laptop behind corporate NAT.

**Foundry** — the source of truth for models. The orchestrator mirrors the project's
deployments and uses that mirror to bind a model to each task.

## Why long-poll leases

Inbound connectivity is the usual reason fleet tools need firewall exceptions. Here the
node calls out, holds the connection open, and receives work on the response. Consequences:

* no NSG rule, no port forwarding, no VPN required for the data path;
* a node that dies stops renewing its lease, and the reaper re-queues its work;
* back-pressure is natural — a saturated node simply stops asking.

The cost is that leases must outlive the longest task. An autonomous run doing several model
round-trips easily exceeds a 60-second default, and the reaper will re-dispatch work that is
still running. `ORCH_LEASE_SECONDS` is the knob.

## Why no model keys exist

The orchestrator authenticates to Foundry with Entra ID to read the deployment catalog. When
it dispatches a task it sends only *coordinates*: endpoint, project, deployment name. The
node then authenticates independently with its own managed identity.

This is the difference between a leaked task payload being embarrassing and being an
incident. It also makes revocation a single RBAC change instead of a credential rotation
across every machine.

## Routing

```
submission ─► infer capabilities from action namespace
           ─► hard filter: offline · saturated · missing capability · label mismatch
           ─► score: framework affinity · spare capacity · capability specificity
                     label overlap · heartbeat freshness
           ─► bind a Foundry deployment if the task needs one
```

`POST /api/v1/tasks/preview-routing` runs the whole decision and explains it without
queueing anything. When routing surprises you, that endpoint is the answer.

If a task needs a model Foundry does not currently publish, it stays `pending` with an
explanatory event and starts by itself once a matching deployment appears.

## Workflows

A workflow is a DAG validated at submission time with Kahn's algorithm — cycles and unknown
dependencies are rejected before anything is queued. Each step becomes an independently
routed task, so one workflow naturally spans several machines and execution backends.

Steps do **not** receive their dependencies' output automatically. Each step carries its own
payload. This keeps routing and retries simple; chaining data between steps is the
operator's job, usually from a script.

## Adapters

Everything a node can execute sits behind one protocol: `handles()`, `capabilities()`,
`execute()`. The runner asks each adapter whether it handles an action and dispatches to the
first that says yes.

| Adapter | Actions | Character |
| --- | --- | --- |
| `BuiltinAdapter` | `fs.*`, `http.*`, `shell.exec`, `foundry.chat` | scripted, narrow, always available |
| `ScoutAdapter` / `ClawdbotAdapter` | `research.*`, `ops.*`, ... | forwards to a local runtime over HTTP |
| `AutonomousAdapter` | `agent.*` | goal-driven, picks its own tools |

The framework bridge protocol is deliberately trivial — `POST {endpoint}/run` with a JSON
task. `agent_bridge/foundry_bridge.py` implements it on top of Microsoft Foundry agents,
which is how a cloud-hosted agent becomes just another execution target.

## The autonomous loop

```
goal ─► model + tool schema ─► tool calls ─► execute ─► results back to model ─► repeat
                                     │                                             │
                                     └──────────── bounded by max_steps ───────────┘
```

Tool failures are returned to the model as observations rather than raised. A failing
compile becomes stderr the model reads and repairs. This is the difference between an agent
that gives up and one that converges.

Every tool call is recorded with its arguments and outcome. The trace is what the agent
*did*, independent of what its summary *claims* — worth more than the summary when
diagnosing a bad run.

## Trust boundaries

Capability grows with risk, so each layer is constrained explicitly.

| Layer | What it can reach | What stops it |
| --- | --- | --- |
| `fs.*`, autonomous file tools | one workspace root | real-path resolution; absolute, UNC and traversal rejected |
| `shell.exec` | pre-approved commands | name-to-argv allow-list, `shell=False` |
| `open_file` | documents | extension allow-list, so no executable reaches a file association |
| `http.request`, browser | public internet | loopback, link-local and metadata addresses blocked |
| desktop control | the interactive session | application and key allow-lists |
| `run_code` | an interpreter | runtime allow-list, workspace-resolved path, timeout |

The instance metadata endpoint is blocked specifically because a node's managed identity
token is obtainable there. An agent influenced by hostile page content must not be able to
navigate to it and read the result back into its own transcript.

## Accepted risks

Two are inherent to the capability rather than fixable:

**Web content re-enters the planning loop.** `browser_read` returns attacker-controlled text
straight into the model's context. The system prompt frames it as data, which reduces the
risk without eliminating it. Narrow `allowed_domains` for anything unattended.

**A program started by `run_code` is not sandboxed.** The file guards apply to the *path* of
the program, not to what the process does once running. Enable it only on disposable
machines.

Both are opt-in per node and off by default.

## Scale-out seams

The build is one process over SQLite. The places that would change:

* `orchestrator/db.py` — swap for Azure SQL or PostgreSQL. The guarded
  `UPDATE ... WHERE status = 'pending'` claim already makes leasing safe for multiple
  replicas.
* `orchestrator/scheduler.py` — the reaper is idempotent and can run in every replica.
* `agents.secret` — one column, the single touch point for moving to Key Vault.
* `orchestrator/security.py` — one dependency to swap the operator API key for Entra ID
  bearer tokens.

Long-poll leasing already scales to hundreds of nodes without inbound ports, so the node
side needs no change.
