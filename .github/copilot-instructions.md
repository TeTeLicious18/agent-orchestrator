# Copilot instructions

Control plane plus agent runtime for a fleet of autonomous AI agents on Azure VMs and
network PCs. [AGENTS.md](../AGENTS.md) is the full guide; `orchestrator/AGENTS.md` and
`agent_node/AGENTS.md` cover their subtrees. This file is the short version.

## Before you finish

```bash
python -m ruff check .
python -m pytest -q
```

Both must pass. The suite runs in about three seconds, so there is no reason to skip it.

## Style

Python 3.11+, `from __future__ import annotations`, built-in generics and `X | None`.
Comments say *why*, never *what*, and one line is usually enough. Docstrings state the trust
boundary or the failure mode, not the signature. Match the surrounding code rather than
introducing a new idiom.

## Things that will break if you are careless

* **SQL** is always parameterised. Variable-length `IN` clauses interpolate `?` placeholders
  only, with a `# noqa: S608` explaining why. Never interpolate a value.
* **The dashboard** builds DOM nodes through helpers. `innerHTML` is never used, because
  task titles and model output are attacker-influenced.
* **Path handling** in adapters goes through `_resolve`. Skipping it reopens directory
  traversal.
* **Allow-lists** map a name to a fixed argv. A model must never be able to supply command
  text, and `shell=False` stays.
* **`tests/conftest.py`** pins its environment because `config.py` calls `load_dotenv()`.
  Break that and the suite starts calling a live Foundry project.

## Secrets

`.env` and `node.env` are gitignored and must never be committed, quoted in a comment, or
echoed into logs. `*.example` files show the shape. No model key exists anywhere in this
system — nodes authenticate to Foundry with their own managed identity, and the orchestrator
sends only deployment coordinates. Keep it that way.

## Changing the API

Update the schema in `orchestrator/models.py`, the logic in the owning module, the route in
`main.py`, a test, then regenerate the contract:

```bash
python tools/export_openapi.py
```

Commit the regenerated `docs/openapi.json` in the same change.
