# Control plane

FastAPI service that owns the fleet: registration, routing, leasing, workflow
orchestration, audit and metrics. Read [../AGENTS.md](../AGENTS.md) first for repository-wide
conventions, and [../docs/architecture.md](../docs/architecture.md) for why the pieces fit
together this way.

## Modules

| File | Responsibility |
| --- | --- |
| `main.py` | Route definitions and application wiring. Thin — logic lives below it |
| `models.py` | Pydantic schemas and enums. The contract, including DAG validation |
| `db.py` | SQLite access, schema bootstrap, transaction helper |
| `security.py` | Operator and agent authentication, HMAC canonicalisation, nonce cache |
| `registry.py` | Agent lifecycle, heartbeats, liveness expiry |
| `routing.py` | Capability inference, filtering and scoring for agents and models |
| `tasks.py` | Task creation, state transitions, telemetry events |
| `scheduler.py` | Lease grants and renewals, the reaper for expiry and timeouts |
| `workflows.py` | DAG creation, unblocking, cancellation, result aggregation |
| `foundry.py` | Deployment catalog sync and capability normalisation |
| `audit.py` · `metrics.py` | Governance trail and aggregate KPIs |
| `static/` | Dashboard. Plain JS, no build step |

## Rules that are not obvious

**Every SQL statement is parameterised.** Where an `IN` clause needs a variable number of
values, the f-string interpolates `?` placeholders only, never values, and carries a
`# noqa: S608` with that reason. If you add a query, follow the pattern — the linter rule
stays on so a genuine injection gets caught.

**State transitions belong in `tasks.py` and `scheduler.py`.** Routes validate and delegate.
A route that writes task state directly will diverge from the reaper.

**`db.transaction()` is the only write path.** It gives atomicity and consistent locking
behaviour; bare `execute` outside it will eventually deadlock under concurrent leases.

**The lease claim is a guarded update.** `UPDATE ... WHERE status = 'pending'` is what makes
double-dispatch impossible, and what would keep leasing correct across multiple orchestrator
replicas. Do not replace it with read-then-write.

## Dashboard

`static/app.js` builds DOM nodes through the `el()` helper. `innerHTML` is never used —
task titles, agent names and model output are all attacker-influenced strings.

The task form derives a structured submission from one plain-language field; advanced
controls override the derived values. When adding a field, add it to `readSubmission()` and
leave it optional, so the simple path keeps working.

## Adding an endpoint

1. Schema in `models.py`.
2. Logic in the module that owns the entity.
3. Route in `main.py`, with `require_operator` or `require_agent`.
4. Test in `tests/test_orchestration.py`.
5. `python tools/export_openapi.py` and commit the regenerated spec.

## Tests

`tests/conftest.py` pins its environment before importing the app, including empty Foundry
endpoints, because `config.py` calls `load_dotenv()` and would otherwise inherit a
developer's `.env`. Preserve that when adding settings, or the suite stops being hermetic
and starts making network calls.
