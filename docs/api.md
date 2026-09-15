# API reference

The machine-readable contract is [`openapi.json`](openapi.json), exported from the running
application. A live copy is served at `/openapi.json`, with Swagger UI at `/docs`.

Regenerate the checked-in copy after changing any route or model:

```bash
python tools/export_openapi.py
```

## Authentication

Two kinds of caller, two mechanisms.

**Operators** send a static API key. In production this is the single place to swap for
Entra ID bearer tokens — see `require_operator` in `orchestrator/security.py`.

```
X-API-Key: <key from ORCH_API_KEYS>
```

**Agents** sign every request. Enrolment uses a one-time bootstrap token; afterwards the
node holds a per-agent secret issued by the server.

```
X-Agent-Id:   <agent id>
X-Timestamp:  <ISO-8601, UTC>
X-Nonce:      <single use>
X-Signature:  v1=<hex HMAC-SHA256>
```

The signed message is the newline-joined canonical request:

```
v1 \n METHOD \n /url/path \n timestamp \n nonce \n sha256(body)
```

Three properties follow. The body cannot be tampered with, because its hash is signed. A
captured request cannot be replayed, because the nonce is single-use inside a bounded skew
window. And one agent cannot act as another, because the agent id in the path must match the
signing identity.

## Endpoints

### Agent plane — HMAC signed

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/agents/register` | Enrol with the bootstrap token; returns the signing secret |
| POST | `/api/v1/agents/{id}/heartbeat` | Liveness and load reporting |
| POST | `/api/v1/agents/{id}/lease` | Long-poll for work |
| POST | `/api/v1/tasks/{id}/progress` | Stream telemetry while executing |
| POST | `/api/v1/tasks/{id}/complete` | Report the terminal result and token usage |

Progress and completion are accepted only from the agent currently holding the lease.

### Operator plane — API key

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/agents` · `/{id}` | Fleet inventory |
| POST | `/api/v1/agents/{id}/drain` | Stop scheduling new work to a node |
| DELETE | `/api/v1/agents/{id}` | Deregister |
| POST | `/api/v1/tasks` | Submit a task |
| POST | `/api/v1/tasks/preview-routing` | Explain the routing decision without queueing |
| GET | `/api/v1/tasks` · `/{id}` · `/{id}/events` | Status and telemetry |
| POST | `/api/v1/tasks/{id}/cancel` | Cancel |
| POST | `/api/v1/workflows` | Submit a DAG |
| GET | `/api/v1/workflows` · `/{id}` | Status and aggregated result |
| POST | `/api/v1/workflows/{id}/cancel` | Cancel a workflow |
| GET | `/api/v1/models` · `/{name}` | Foundry deployment catalog |
| POST | `/api/v1/models/sync` | Force a catalog re-sync |
| GET | `/api/v1/audit` | Governance trail |
| GET | `/api/v1/metrics/summary` | Fleet KPIs and token usage |

`GET /healthz` is unauthenticated and intended for probes.

## Submitting a task

Only `title` and `action` are required. Everything else narrows placement.

```json
{
  "title": "Summarise the incident report",
  "action": "agent.do",
  "payload": { "goal": "Read incident.md and write a three-bullet summary" },
  "required_capabilities": [],
  "preferred_framework": null,
  "target_agent_id": null,
  "label_selector": { "tier": "research" },
  "priority": 5,
  "max_attempts": 2,
  "timeout_seconds": 900,
  "requested_model": null,
  "required_model_capabilities": ["chat"]
}
```

Capabilities left empty are inferred from the action namespace via `CAPABILITY_HINTS` in
`orchestrator/routing.py`. `target_agent_id` pins the task to one machine; `label_selector`
narrows to a group. Either `requested_model` or `required_model_capabilities` causes a
Foundry deployment to be bound — without one of them the task runs without model access.

### Actions

| Action | Adapter | Payload |
| --- | --- | --- |
| `agent.do` | autonomous | `goal` |
| `foundry.chat` | builtin | `prompt` and optional `system`, or `messages` |
| `fs.read` · `fs.write` · `fs.list` | builtin | `path`, and `content` for writes |
| `shell.exec` | builtin | `command` from the node allow-list, optional `args` |
| `http.request` | builtin | `url`, `method`, `headers`, `body` |
| `research.*` · `ops.*` · `automation.*` | framework bridge | forwarded verbatim |

There is no `llm.complete`. Model calls go through `foundry.chat` or `agent.do`.

## Submitting a workflow

```json
{
  "name": "Nightly report",
  "steps": [
    { "step_id": "gather",  "title": "Gather", "action": "research.web",
      "payload": { "prompt": "..." } },
    { "step_id": "write",   "title": "Write",  "action": "foundry.chat",
      "depends_on": ["gather"], "payload": { "prompt": "..." },
      "required_model_capabilities": ["chat"] }
  ]
}
```

Cycles and unknown dependencies are rejected at submission. Each step is routed
independently, so a workflow can span several machines.

## Task lifecycle

```
pending ──lease──► assigned ──progress──► running ──┬─► succeeded
   ▲                   │                            ├─► failed (after max_attempts)
   └── retry / lease expiry ────────────────────────┴─► timed_out
```

`blocked` is the additional state for workflow steps waiting on a dependency.

## Errors

| Status | Meaning |
| --- | --- |
| 401 | Missing or invalid credential, stale timestamp, or replayed nonce |
| 403 | Signing identity does not match the agent id in the path |
| 404 | Unknown agent, task, workflow or deployment |
| 409 | Task already leased, or a state transition that is not allowed |
| 422 | Schema validation failed, including workflow cycles |

## Diagnosing a failed task

`GET /api/v1/tasks/{id}/events` is the highest-value endpoint when something goes wrong. It
returns the ordered telemetry — routing decision, model binding, per-step progress and the
adapter error — which is usually enough to identify the cause without touching the node.
