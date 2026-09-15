"""Bridge between an agent node and Microsoft Foundry agents.

``FrameworkBridge`` in ``agent_node/adapters/framework.py`` posts a task to
``{endpoint}/run``. This service speaks that contract and forwards the work to a
Foundry agent, so a Scout/Clawdbot-shaped adapter can drive a Foundry agent
without the node knowing anything about Foundry agent APIs.

Run it on the node, bound to loopback only::

    python -m uvicorn agent_bridge.foundry_bridge:app --host 127.0.0.1 --port 7801

Configuration (environment variables):

``FOUNDRY_PROJECT_ENDPOINT``
    Project endpoint, e.g. ``https://<account>.services.ai.azure.com/api/projects/<project>``.
``FOUNDRY_DEFAULT_AGENT``
    Agent name used when no namespace-specific mapping matches.
``FOUNDRY_AGENT_MAP``
    Optional ``namespace=agent`` pairs, comma separated, e.g.
    ``research=web-researcher,ops=ops-executor``.

Authentication uses ``DefaultAzureCredential`` - the VM's managed identity. No
key or token is ever accepted from the caller.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("agent.bridge.foundry")

app = FastAPI(title="Foundry agent bridge")

_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
_DEFAULT_AGENT = os.getenv("FOUNDRY_DEFAULT_AGENT", "").strip()


def _parse_agent_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        namespace, separator, agent = pair.partition("=")
        if separator and namespace.strip() and agent.strip():
            mapping[namespace.strip().lower()] = agent.strip()
    return mapping


_AGENT_MAP = _parse_agent_map(os.getenv("FOUNDRY_AGENT_MAP", ""))

_client: AIProjectClient | None = None


def _openai_client() -> Any:
    """One authenticated client per process, created lazily."""
    global _client
    if _client is None:
        if not _ENDPOINT:
            raise HTTPException(status_code=500, detail="FOUNDRY_PROJECT_ENDPOINT is not configured")
        _client = AIProjectClient(endpoint=_ENDPOINT, credential=DefaultAzureCredential())
    return _client.get_openai_client()


def _agent_for(action: str) -> str:
    namespace = action.split(".", 1)[0].lower()
    agent = _AGENT_MAP.get(namespace) or _DEFAULT_AGENT
    if not agent:
        raise HTTPException(
            status_code=400,
            detail=f"no Foundry agent mapped for action '{action}' and no FOUNDRY_DEFAULT_AGENT set",
        )
    return agent


def _build_input(request: RunRequest) -> str:
    parts = [f"Task: {request.title or request.action}", f"Action: {request.action}"]
    payload = request.input or {}
    prompt = payload.get("prompt") or payload.get("query") or payload.get("instruction")
    if isinstance(prompt, str) and prompt.strip():
        parts.append(prompt.strip())
    else:
        parts.append(f"Input: {payload}")
    return "\n\n".join(parts)


class RunRequest(BaseModel):
    task_id: str
    action: str
    title: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    # Deployment coordinates chosen by the orchestrator. The Foundry agent owns
    # its own model, so this is recorded for telemetry rather than applied.
    model: dict[str, Any] | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "endpoint_configured": bool(_ENDPOINT),
        "default_agent": _DEFAULT_AGENT or None,
        "agent_map": _AGENT_MAP,
    }


@app.post("/run")
def run(request: RunRequest) -> dict[str, Any]:
    agent = _agent_for(request.action)
    log.info("task %s (%s) -> Foundry agent '%s'", request.task_id, request.action, agent)

    try:
        response = _openai_client().responses.create(
            extra_body={"agent_reference": {"type": "agent_reference", "name": agent}},
            input=_build_input(request),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to the node as a task failure
        log.exception("Foundry agent '%s' failed for task %s", agent, request.task_id)
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from None

    usage = getattr(response, "usage", None)
    return {
        "agent": agent,
        "summary": response.output_text,
        "usage": {
            "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        },
    }
