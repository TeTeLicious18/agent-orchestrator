"""Microsoft Foundry integration: catalog sync, model routing and usage."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from orchestrator import foundry
from orchestrator.models import ModelStatus

from .conftest import CATALOG, FOUNDRY_ENDPOINT
from .test_orchestration import agent_post, register

# An agent may only receive model-bound work if it advertises foundry.inference.
INFERENCE_AGENT = {
    "agent_id": "scout-vm-02",
    "name": "Scout Agent with Foundry",
    "framework": "scout",
    "platform": "azure_vm",
    "host": "vm-02",
    "capabilities": ["research", "browser.automation", "llm.reasoning", "foundry.inference"],
    "labels": {"region": "westeurope"},
    "max_concurrency": 2,
}

OFFLINE_MODEL_AGENT = {
    "agent_id": "legacy-pc-01",
    "name": "Legacy Agent without Foundry",
    "framework": "clawdbot",
    "platform": "network_pc",
    "host": "pc-legacy",
    "capabilities": ["research", "browser.automation", "llm.reasoning"],
    "labels": {},
    "max_concurrency": 1,
}


def submit(client: TestClient, operator: dict[str, str], **overrides: Any) -> dict[str, Any]:
    body = {"title": "Model backed task", "action": "research.web"}
    body.update(overrides)
    response = client.post("/api/v1/tasks", json=body, headers=operator)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Catalog sync
# --------------------------------------------------------------------------- #
def test_sync_mirrors_foundry_deployments(client: TestClient, operator: dict[str, str], catalog) -> None:
    assert catalog.status == "synced"
    assert catalog.discovered == len(CATALOG)
    assert catalog.added == len(CATALOG)

    models = client.get("/api/v1/models", headers=operator).json()
    names = {model["name"] for model in models}
    assert names == {entry["name"] for entry in CATALOG}
    assert all(model["endpoint"] == FOUNDRY_ENDPOINT for model in models)
    assert all(model["status"] == "available" for model in models)


def test_capability_flags_are_normalised(client: TestClient, operator: dict[str, str], catalog) -> None:
    gpt4o = client.get("/api/v1/models/gpt-4o-orchestrator", headers=operator).json()
    assert set(gpt4o["capabilities"]) >= {"chat", "tool-calling", "json-mode", "vision"}
    # "embeddings": "false" must not become a capability.
    assert "embeddings" not in gpt4o["capabilities"]

    # Reasoning families are inferred from the model name when not flagged.
    o4 = client.get("/api/v1/models/o4-mini-reasoning", headers=operator).json()
    assert "reasoning" in o4["capabilities"]

    embeddings = client.get("/api/v1/models/text-embedding-3-large", headers=operator).json()
    assert "embeddings" in embeddings["capabilities"]


def test_resync_updates_rather_than_duplicates(client: TestClient, catalog) -> None:
    second = foundry.sync()
    assert second.added == 0
    assert second.updated == len(CATALOG)
    assert len(foundry.list_deployments()) == len(CATALOG)


def test_removed_deployment_is_retired_not_deleted(client: TestClient, catalog) -> None:
    remaining = [
        {
            "name": entry["name"],
            "model_name": entry["model_name"],
            "publisher": entry.get("publisher"),
            "model_version": entry.get("model_version"),
            "sku": entry.get("sku"),
            "deployment_type": None,
            "endpoint": FOUNDRY_ENDPOINT,
            "project": "orchestrator",
            "raw_capabilities": entry["capabilities"],
        }
        for entry in CATALOG
        if entry["name"] != "o4-mini-reasoning"
    ]
    _, _, retired = foundry._persist(remaining, "foundry")
    assert retired == 1

    retired_model = foundry.get("o4-mini-reasoning")
    assert retired_model is not None
    assert retired_model.status is ModelStatus.UNAVAILABLE
    assert len(foundry.list_deployments(status=ModelStatus.AVAILABLE)) == len(CATALOG) - 1


def test_models_endpoint_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/models").status_code == 401
    assert client.post("/api/v1/models/sync").status_code == 401


def test_sync_endpoint_is_audited(client: TestClient, operator: dict[str, str]) -> None:
    result = client.post("/api/v1/models/sync", headers=operator).json()
    assert result["status"] == "synced"
    actions = {entry["action"] for entry in client.get("/api/v1/audit", headers=operator).json()}
    assert "models.sync" in actions


# --------------------------------------------------------------------------- #
# Model aware routing
# --------------------------------------------------------------------------- #
def test_preview_reports_agent_and_model(client: TestClient, operator: dict[str, str], catalog) -> None:
    register(client, INFERENCE_AGENT)
    preview = client.post(
        "/api/v1/tasks/preview-routing",
        json={
            "title": "Summarise findings",
            "action": "research.web",
            "required_model_capabilities": ["chat", "tool-calling"],
        },
        headers=operator,
    ).json()
    assert preview["agent_id"] == INFERENCE_AGENT["agent_id"]
    assert preview["model_deployment"] == "gpt-4o-orchestrator"


def test_agent_without_inference_capability_is_not_eligible(
    client: TestClient, operator: dict[str, str], catalog
) -> None:
    register(client, OFFLINE_MODEL_AGENT)
    preview = client.post(
        "/api/v1/tasks/preview-routing",
        json={
            "title": "Needs a model",
            "action": "research.web",
            "required_model_capabilities": ["chat"],
        },
        headers=operator,
    ).json()
    assert preview["agent_id"] is None
    assert "foundry.inference" in preview["reason"]


def test_capability_selects_the_right_deployment(
    client: TestClient, operator: dict[str, str], catalog
) -> None:
    register(client, INFERENCE_AGENT)
    preview = client.post(
        "/api/v1/tasks/preview-routing",
        json={
            "title": "Embed the corpus",
            "action": "research.web",
            "required_model_capabilities": ["embeddings"],
        },
        headers=operator,
    ).json()
    assert preview["model_deployment"] == "text-embedding-3-large"


def test_unsatisfiable_model_capability_is_explained(
    client: TestClient, operator: dict[str, str], catalog
) -> None:
    register(client, INFERENCE_AGENT)
    preview = client.post(
        "/api/v1/tasks/preview-routing",
        json={
            "title": "Generate an image",
            "action": "research.web",
            "required_model_capabilities": ["image-generation"],
        },
        headers=operator,
    ).json()
    assert preview["model_deployment"] is None
    assert "image-generation" in preview["model_reason"]


# --------------------------------------------------------------------------- #
# Binding, dispatch and usage
# --------------------------------------------------------------------------- #
def test_lease_carries_a_keyless_model_binding(
    client: TestClient, operator: dict[str, str], catalog
) -> None:
    secret = register(client, INFERENCE_AGENT)
    task = submit(client, operator, required_model_capabilities=["chat"])

    leased = agent_post(
        client,
        INFERENCE_AGENT["agent_id"],
        secret,
        f"/api/v1/agents/{INFERENCE_AGENT['agent_id']}/lease",
        {"max_tasks": 1, "wait_seconds": 0},
    ).json()
    assert len(leased) == 1
    binding = leased[0]["model"]
    assert binding["deployment_name"] == "gpt-4o-orchestrator"
    assert binding["endpoint"] == FOUNDRY_ENDPOINT
    assert binding["auth"] == "entra-id"
    # No secret material may ever reach an agent.
    assert not any("key" in field or "secret" in field or "token" in field for field in binding)

    stored = client.get(f"/api/v1/tasks/{task['task_id']}", headers=operator).json()
    assert stored["model_deployment"] == "gpt-4o-orchestrator"

    kinds = [
        event["kind"]
        for event in client.get(f"/api/v1/tasks/{task['task_id']}/events", headers=operator).json()
    ]
    assert "model_bound" in kinds


def test_pinned_deployment_is_honoured(client: TestClient, operator: dict[str, str], catalog) -> None:
    secret = register(client, INFERENCE_AGENT)
    submit(client, operator, requested_model="o4-mini-reasoning")
    leased = agent_post(
        client,
        INFERENCE_AGENT["agent_id"],
        secret,
        f"/api/v1/agents/{INFERENCE_AGENT['agent_id']}/lease",
        {"max_tasks": 1, "wait_seconds": 0},
    ).json()
    assert leased[0]["model"]["deployment_name"] == "o4-mini-reasoning"


def test_task_stays_pending_when_no_model_matches(
    client: TestClient, operator: dict[str, str], catalog
) -> None:
    secret = register(client, INFERENCE_AGENT)
    task = submit(client, operator, requested_model="does-not-exist")
    leased = agent_post(
        client,
        INFERENCE_AGENT["agent_id"],
        secret,
        f"/api/v1/agents/{INFERENCE_AGENT['agent_id']}/lease",
        {"max_tasks": 1, "wait_seconds": 0},
    ).json()
    assert leased == []

    refreshed = client.get(f"/api/v1/tasks/{task['task_id']}", headers=operator).json()
    assert refreshed["status"] == "pending"
    events = client.get(f"/api/v1/tasks/{task['task_id']}/events", headers=operator).json()
    assert events[-1]["kind"] == "model_pending"
    assert "not in the catalog" in events[-1]["message"]


def test_token_usage_is_recorded_and_aggregated(
    client: TestClient, operator: dict[str, str], catalog
) -> None:
    secret = register(client, INFERENCE_AGENT)
    task = submit(client, operator, required_model_capabilities=["chat"])
    agent_id = INFERENCE_AGENT["agent_id"]
    agent_post(client, agent_id, secret, f"/api/v1/agents/{agent_id}/lease", {"max_tasks": 1, "wait_seconds": 0})
    agent_post(
        client,
        agent_id,
        secret,
        f"/api/v1/tasks/{task['task_id']}/complete",
        {
            "status": "succeeded",
            "result": {"content": "done"},
            "usage": {"prompt_tokens": 420, "completion_tokens": 80},
        },
    )

    finished = client.get(f"/api/v1/tasks/{task['task_id']}", headers=operator).json()
    assert finished["prompt_tokens"] == 420
    assert finished["completion_tokens"] == 80

    summary = client.get("/api/v1/metrics/summary", headers=operator).json()
    assert summary["models_total"] == len(CATALOG)
    assert summary["models_available"] == len(CATALOG)
    assert summary["tokens_by_model"]["gpt-4o-orchestrator"] == 500
    assert summary["tokens_total"] == 500


def test_workflow_steps_can_request_different_models(
    client: TestClient, operator: dict[str, str], catalog
) -> None:
    register(client, INFERENCE_AGENT)
    workflow = client.post(
        "/api/v1/workflows",
        json={
            "name": "Mixed model workflow",
            "steps": [
                {
                    "step_id": "embed",
                    "title": "Embed",
                    "action": "research.web",
                    "required_model_capabilities": ["embeddings"],
                },
                {
                    "step_id": "reason",
                    "title": "Reason",
                    "action": "research.web",
                    "requested_model": "o4-mini-reasoning",
                    "depends_on": ["embed"],
                },
            ],
        },
        headers=operator,
    ).json()
    steps = {task["step_id"]: task for task in workflow["tasks"]}
    assert steps["embed"]["required_model_capabilities"] == ["embeddings"]
    assert steps["reason"]["requested_model"] == "o4-mini-reasoning"


def test_tasks_without_model_requirements_are_unaffected(
    client: TestClient, operator: dict[str, str], catalog
) -> None:
    """Non-model work must still route to agents that never touch Foundry."""
    secret = register(client, OFFLINE_MODEL_AGENT)
    submit(client, operator, title="Plain research")
    agent_id = OFFLINE_MODEL_AGENT["agent_id"]
    leased = agent_post(
        client, agent_id, secret, f"/api/v1/agents/{agent_id}/lease", {"max_tasks": 1, "wait_seconds": 0}
    ).json()
    assert len(leased) == 1
    assert leased[0]["model"] is None
