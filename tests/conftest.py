"""Test fixtures: an isolated orchestrator instance backed by a temp database."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

API_KEY = "test-operator-key"
BOOTSTRAP = "test-bootstrap-token"
FOUNDRY_ENDPOINT = "https://contoso-ai.services.ai.azure.com/api/projects/orchestrator"

CATALOG = [
    {
        "name": "gpt-4o-orchestrator",
        "model_name": "gpt-4o",
        "publisher": "OpenAI",
        "model_version": "2024-11-20",
        "sku": "GlobalStandard",
        "endpoint": FOUNDRY_ENDPOINT,
        "capabilities": {
            "chatCompletion": "true",
            "assistants": "true",
            "jsonObjectResponse": "true",
            "embeddings": "false",
        },
    },
    {
        "name": "o4-mini-reasoning",
        "model_name": "o4-mini",
        "publisher": "OpenAI",
        "endpoint": FOUNDRY_ENDPOINT,
        "capabilities": {"chatCompletion": "true"},
    },
    {
        "name": "text-embedding-3-large",
        "model_name": "text-embedding-3-large",
        "publisher": "OpenAI",
        "endpoint": FOUNDRY_ENDPOINT,
        "capabilities": {"embeddings": "true"},
    },
]

_tmpdir = tempfile.mkdtemp(prefix="orch-test-")
_catalog_path = os.path.join(_tmpdir, "catalog.json")
with open(_catalog_path, "w", encoding="utf-8") as _handle:
    json.dump(CATALOG, _handle)

os.environ["ORCH_API_KEYS"] = API_KEY
os.environ["ORCH_BOOTSTRAP_TOKEN"] = BOOTSTRAP
os.environ["ORCH_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["ORCH_DISABLE_SCHEDULER"] = "1"
os.environ["ORCH_FOUNDRY_CATALOG_FILE"] = _catalog_path
os.environ["ORCH_MODEL_PREFERENCE"] = "gpt-4o-orchestrator"
# Set rather than unset: config.py calls load_dotenv(), which would otherwise let a
# developer's local .env point the tests at a live Foundry project.
os.environ["FOUNDRY_PROJECT_ENDPOINT"] = ""
os.environ["ORCH_FOUNDRY_ENDPOINT"] = ""
os.environ["ORCH_LEASE_SECONDS"] = "60"
os.environ["ORCH_DEFAULT_TASK_TIMEOUT"] = "900"

from fastapi.testclient import TestClient  # noqa: E402

from orchestrator import db, foundry  # noqa: E402
from orchestrator.main import app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as test_client:
        with db.transaction() as conn:
            conn.execute("DELETE FROM task_events")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM workflows")
            conn.execute("DELETE FROM agents")
            conn.execute("DELETE FROM audit_log")
            conn.execute("DELETE FROM model_deployments")
        yield test_client


@pytest.fixture()
def catalog(client: TestClient):
    """A synced Foundry catalog, sourced from the local fixture file."""
    return foundry.sync()


@pytest.fixture()
def operator() -> dict[str, str]:
    return {"X-API-Key": API_KEY}
