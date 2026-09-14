"""End-to-end control plane tests: auth, routing, dispatch and workflows."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

from orchestrator.security import sign

from .conftest import BOOTSTRAP

SCOUT_AGENT = {
    "agent_id": "scout-vm-01",
    "name": "Scout Research Agent",
    "framework": "scout",
    "platform": "azure_vm",
    "host": "vm-01",
    "capabilities": ["research", "browser.automation", "llm.reasoning"],
    "labels": {"region": "westeurope"},
    "max_concurrency": 2,
}

OPS_AGENT = {
    "agent_id": "clawdbot-pc-01",
    "name": "Clawdbot Ops Agent",
    "framework": "clawdbot",
    "platform": "network_pc",
    "host": "pc-01",
    "capabilities": ["shell.exec", "filesystem", "system.admin"],
    "labels": {"region": "onprem"},
    "max_concurrency": 1,
}


def register(client: TestClient, registration: dict[str, Any]) -> str:
    response = client.post(
        "/api/v1/agents/register",
        json=registration,
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert response.status_code == 200, response.text
    return response.json()["agent_secret"]


def agent_headers(agent_id: str, secret: str, path: str, body: bytes, method: str = "POST") -> dict[str, str]:
    timestamp = datetime.now(timezone.utc).isoformat()
    nonce = secrets.token_urlsafe(12)
    return {
        "X-Agent-Id": agent_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": sign(secret, method, path, timestamp, nonce, body),
        "Content-Type": "application/json",
    }


def agent_post(client: TestClient, agent_id: str, secret: str, path: str, payload: dict[str, Any]):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(path, content=body, headers=agent_headers(agent_id, secret, path, body))


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def test_operator_endpoints_require_api_key(client: TestClient) -> None:
    assert client.get("/api/v1/agents").status_code == 401
    assert client.get("/api/v1/agents", headers={"X-API-Key": "wrong"}).status_code == 401


def test_registration_requires_bootstrap_token(client: TestClient) -> None:
    response = client.post("/api/v1/agents/register", json=SCOUT_AGENT)
    assert response.status_code == 401


def test_tampered_signature_is_rejected(client: TestClient) -> None:
    secret = register(client, SCOUT_AGENT)
    path = f"/api/v1/agents/{SCOUT_AGENT['agent_id']}/heartbeat"
    body = json.dumps({"status": "online", "active_tasks": 0, "metrics": {}}).encode()
    headers = agent_headers(SCOUT_AGENT["agent_id"], secret, path, body)
    tampered = json.dumps({"status": "online", "active_tasks": 99, "metrics": {}}).encode()
    assert client.post(path, content=tampered, headers=headers).status_code == 401


def test_nonce_replay_is_rejected(client: TestClient) -> None:
    secret = register(client, SCOUT_AGENT)
    path = f"/api/v1/agents/{SCOUT_AGENT['agent_id']}/heartbeat"
    body = json.dumps({"status": "online", "active_tasks": 0, "metrics": {}}).encode()
    headers = agent_headers(SCOUT_AGENT["agent_id"], secret, path, body)
    assert client.post(path, content=body, headers=headers).status_code == 200
    assert client.post(path, content=body, headers=headers).status_code == 401


def test_agent_cannot_impersonate_another_node(client: TestClient) -> None:
    scout_secret = register(client, SCOUT_AGENT)
    register(client, OPS_AGENT)
    path = f"/api/v1/agents/{OPS_AGENT['agent_id']}/heartbeat"
    payload = {"status": "online", "active_tasks": 0, "metrics": {}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = agent_headers(SCOUT_AGENT["agent_id"], scout_secret, path, body)
    assert client.post(path, content=body, headers=headers).status_code == 403


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def test_routing_preview_picks_capability_match(client: TestClient, operator: dict[str, str]) -> None:
    register(client, SCOUT_AGENT)
    register(client, OPS_AGENT)

    preview = client.post(
        "/api/v1/tasks/preview-routing",
        json={"title": "Look something up", "action": "research.web"},
        headers=operator,
    ).json()
    assert preview["agent_id"] == "scout-vm-01"

    preview = client.post(
        "/api/v1/tasks/preview-routing",
        json={"title": "Run a script", "action": "shell.exec"},
        headers=operator,
    ).json()
    assert preview["agent_id"] == "clawdbot-pc-01"


def test_routing_reports_capability_gap(client: TestClient, operator: dict[str, str]) -> None:
    register(client, SCOUT_AGENT)
    preview = client.post(
        "/api/v1/tasks/preview-routing",
        json={"title": "Patch the fleet", "action": "sysadmin.patch"},
        headers=operator,
    ).json()
    assert preview["agent_id"] is None
    assert "system.admin" in preview["reason"]


def test_label_selector_is_enforced(client: TestClient, operator: dict[str, str]) -> None:
    register(client, SCOUT_AGENT)
    preview = client.post(
        "/api/v1/tasks/preview-routing",
        json={
            "title": "Region pinned research",
            "action": "research.web",
            "label_selector": {"region": "eastus"},
        },
        headers=operator,
    ).json()
    assert preview["agent_id"] is None


# --------------------------------------------------------------------------- #
# Task lifecycle
# --------------------------------------------------------------------------- #
def test_full_task_lifecycle(client: TestClient, operator: dict[str, str]) -> None:
    secret = register(client, SCOUT_AGENT)
    task = client.post(
        "/api/v1/tasks",
        json={"title": "Summarise release notes", "action": "research.web"},
        headers=operator,
    ).json()
    assert task["status"] == "pending"

    leased = agent_post(
        client,
        SCOUT_AGENT["agent_id"],
        secret,
        f"/api/v1/agents/{SCOUT_AGENT['agent_id']}/lease",
        {"max_tasks": 1, "wait_seconds": 0},
    ).json()
    assert len(leased) == 1
    assert leased[0]["task_id"] == task["task_id"]

    progress = agent_post(
        client,
        SCOUT_AGENT["agent_id"],
        secret,
        f"/api/v1/tasks/{task['task_id']}/progress",
        {"progress": 50, "message": "halfway", "telemetry": {"tokens": 120}},
    )
    assert progress.status_code == 200
    running = client.get(f"/api/v1/tasks/{task['task_id']}", headers=operator).json()
    assert running["status"] == "running"
    assert running["progress"] == 50

    completed = agent_post(
        client,
        SCOUT_AGENT["agent_id"],
        secret,
        f"/api/v1/tasks/{task['task_id']}/complete",
        {"status": "succeeded", "result": {"summary": "done"}, "error": None},
    ).json()
    assert completed["status"] == "succeeded"
    assert completed["result"] == {"summary": "done"}

    events = client.get(f"/api/v1/tasks/{task['task_id']}/events", headers=operator).json()
    kinds = [event["kind"] for event in events]
    assert kinds[0] == "submitted"
    assert "assigned" in kinds and "progress" in kinds and "succeeded" in kinds


def test_task_is_not_leased_twice(client: TestClient, operator: dict[str, str]) -> None:
    secret = register(client, SCOUT_AGENT)
    client.post(
        "/api/v1/tasks",
        json={"title": "Only once", "action": "research.web"},
        headers=operator,
    )
    path = f"/api/v1/agents/{SCOUT_AGENT['agent_id']}/lease"
    first = agent_post(client, SCOUT_AGENT["agent_id"], secret, path, {"max_tasks": 4, "wait_seconds": 0}).json()
    second = agent_post(client, SCOUT_AGENT["agent_id"], secret, path, {"max_tasks": 4, "wait_seconds": 0}).json()
    assert len(first) == 1
    assert second == []


def test_failed_task_is_retried_then_marked_failed(client: TestClient, operator: dict[str, str]) -> None:
    secret = register(client, SCOUT_AGENT)
    task = client.post(
        "/api/v1/tasks",
        json={"title": "Flaky", "action": "research.web", "max_attempts": 2},
        headers=operator,
    ).json()
    lease_path = f"/api/v1/agents/{SCOUT_AGENT['agent_id']}/lease"
    complete_path = f"/api/v1/tasks/{task['task_id']}/complete"

    agent_post(client, SCOUT_AGENT["agent_id"], secret, lease_path, {"max_tasks": 1, "wait_seconds": 0})
    agent_post(client, SCOUT_AGENT["agent_id"], secret, complete_path, {"status": "failed", "error": "boom"})
    assert client.get(f"/api/v1/tasks/{task['task_id']}", headers=operator).json()["status"] == "pending"

    agent_post(client, SCOUT_AGENT["agent_id"], secret, lease_path, {"max_tasks": 1, "wait_seconds": 0})
    agent_post(client, SCOUT_AGENT["agent_id"], secret, complete_path, {"status": "failed", "error": "boom"})
    final = client.get(f"/api/v1/tasks/{task['task_id']}", headers=operator).json()
    assert final["status"] == "failed"
    assert final["attempts"] == 2


def test_concurrency_limit_is_respected(client: TestClient, operator: dict[str, str]) -> None:
    secret = register(client, OPS_AGENT)  # max_concurrency = 1
    for index in range(3):
        client.post(
            "/api/v1/tasks",
            json={"title": f"job {index}", "action": "shell.exec"},
            headers=operator,
        )
    path = f"/api/v1/agents/{OPS_AGENT['agent_id']}/lease"
    leased = agent_post(client, OPS_AGENT["agent_id"], secret, path, {"max_tasks": 5, "wait_seconds": 0}).json()
    assert len(leased) == 1


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #
def test_workflow_dependencies_unblock_in_order(client: TestClient, operator: dict[str, str]) -> None:
    secret = register(client, SCOUT_AGENT)
    workflow = client.post(
        "/api/v1/workflows",
        json={
            "name": "Two step",
            "steps": [
                {"step_id": "gather", "title": "Gather", "action": "research.web"},
                {
                    "step_id": "report",
                    "title": "Report",
                    "action": "report.publish",
                    "depends_on": ["gather"],
                },
            ],
        },
        headers=operator,
    ).json()
    statuses = {task["step_id"]: task["status"] for task in workflow["tasks"]}
    assert statuses == {"gather": "pending", "report": "blocked"}

    lease_path = f"/api/v1/agents/{SCOUT_AGENT['agent_id']}/lease"
    leased = agent_post(client, SCOUT_AGENT["agent_id"], secret, lease_path, {"max_tasks": 2, "wait_seconds": 0}).json()
    assert len(leased) == 1  # the blocked step is invisible to the router

    agent_post(
        client,
        SCOUT_AGENT["agent_id"],
        secret,
        f"/api/v1/tasks/{leased[0]['task_id']}/complete",
        {"status": "succeeded", "result": {"ok": True}},
    )
    refreshed = client.get(f"/api/v1/workflows/{workflow['workflow_id']}", headers=operator).json()
    statuses = {task["step_id"]: task["status"] for task in refreshed["tasks"]}
    assert statuses["gather"] == "succeeded"
    assert statuses["report"] == "pending"
    assert refreshed["status"] == "running"


def test_workflow_rejects_dependency_cycles(client: TestClient, operator: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/workflows",
        json={
            "name": "Cyclic",
            "steps": [
                {"step_id": "a", "title": "A", "action": "echo", "depends_on": ["b"]},
                {"step_id": "b", "title": "B", "action": "echo", "depends_on": ["a"]},
            ],
        },
        headers=operator,
    )
    assert response.status_code == 422


def test_failed_step_cancels_dependents_and_fails_workflow(client: TestClient, operator: dict[str, str]) -> None:
    secret = register(client, SCOUT_AGENT)
    workflow = client.post(
        "/api/v1/workflows",
        json={
            "name": "Failing chain",
            "steps": [
                {"step_id": "gather", "title": "Gather", "action": "research.web", "max_attempts": 1},
                {"step_id": "report", "title": "Report", "action": "report.publish", "depends_on": ["gather"]},
            ],
        },
        headers=operator,
    ).json()

    lease_path = f"/api/v1/agents/{SCOUT_AGENT['agent_id']}/lease"
    leased = agent_post(client, SCOUT_AGENT["agent_id"], secret, lease_path, {"max_tasks": 1, "wait_seconds": 0}).json()
    agent_post(
        client,
        SCOUT_AGENT["agent_id"],
        secret,
        f"/api/v1/tasks/{leased[0]['task_id']}/complete",
        {"status": "failed", "error": "no data"},
    )
    refreshed = client.get(f"/api/v1/workflows/{workflow['workflow_id']}", headers=operator).json()
    statuses = {task["step_id"]: task["status"] for task in refreshed["tasks"]}
    assert statuses == {"gather": "failed", "report": "cancelled"}
    assert refreshed["status"] == "failed"
    assert refreshed["result"]["summary"] == {"failed": 1, "cancelled": 1}


# --------------------------------------------------------------------------- #
# Governance
# --------------------------------------------------------------------------- #
def test_audit_log_records_operator_and_agent_actions(client: TestClient, operator: dict[str, str]) -> None:
    register(client, SCOUT_AGENT)
    client.post("/api/v1/tasks", json={"title": "Audited", "action": "echo"}, headers=operator)
    entries = client.get("/api/v1/audit", headers=operator).json()
    actions = {entry["action"] for entry in entries}
    assert {"agent.register", "task.submit"} <= actions


def test_summary_metrics(client: TestClient, operator: dict[str, str]) -> None:
    register(client, SCOUT_AGENT)
    client.post("/api/v1/tasks", json={"title": "Metric", "action": "research.web"}, headers=operator)
    summary = client.get("/api/v1/metrics/summary", headers=operator).json()
    assert summary["agents_total"] == 1
    assert summary["agents_online"] == 1
    assert summary["tasks_by_status"]["pending"] == 1
    assert summary["agents_by_platform"]["azure_vm"] == 1
