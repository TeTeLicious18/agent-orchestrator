"""Microsoft Foundry model catalog.

Foundry is the source of truth for models. This module keeps a local mirror of
the deployments published in the configured Foundry project so the routing
engine can match tasks to models without an API round-trip on every decision.

Three sources are supported, in priority order:

1. A live Foundry project (``FOUNDRY_PROJECT_ENDPOINT`` + Entra ID) read with
   ``azure-ai-projects``.
2. A local catalog file (``ORCH_FOUNDRY_CATALOG_FILE``) - useful for air-gapped
   demos and for tests.
3. Nothing configured, in which case the catalog stays empty and model-aware
   routing is simply inactive.

No model keys are ever stored: agents authenticate to Foundry with their own
managed identity using the endpoint handed to them in the task assignment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from urllib.parse import urlsplit

from . import db
from .config import get_settings
from .models import ModelDeploymentView, ModelStatus, ModelSyncResult, utcnow_iso

log = logging.getLogger("orchestrator.foundry")

# Foundry reports per-deployment capability flags with SDK specific spellings.
# They are normalised onto the vocabulary the router understands.
_CAPABILITY_ALIASES: dict[str, str] = {
    "chatcompletion": "chat",
    "chatcompletions": "chat",
    "completion": "chat",
    "completions": "chat",
    "assistants": "tool-calling",
    "toolcalling": "tool-calling",
    "functioncalling": "tool-calling",
    "jsonobjectresponse": "json-mode",
    "jsonschemaresponse": "json-mode",
    "responses": "chat",
    "embeddings": "embeddings",
    "imagegenerations": "image-generation",
    "imagegeneration": "image-generation",
    "audio": "audio",
    "realtime": "audio",
    "transcription": "audio",
    "vision": "vision",
    "imageinput": "vision",
    "finetune": "fine-tuning",
    "finetuning": "fine-tuning",
}

# Model families whose capabilities are not always advertised as flags.
_NAME_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("o1", "o3", "o4", "-reasoning", "deepseek-r1", "phi-4-reasoning"), "reasoning"),
    (("gpt-4o", "gpt-4.1", "gpt-5", "vision", "llama-3.2-90b"), "vision"),
    (("embedding", "embed"), "embeddings"),
    (("whisper", "tts", "realtime"), "audio"),
    (("dall-e", "flux", "stable-diffusion"), "image-generation"),
)


def _normalise_capabilities(raw: Any, model_name: str) -> list[str]:
    capabilities: set[str] = set()

    items: list[str] = []
    if isinstance(raw, dict):
        # Foundry returns {"chatCompletion": "true", "embeddings": "false"}.
        items = [key for key, value in raw.items() if str(value).lower() not in {"false", "0", "none"}]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]

    for item in items:
        key = str(item).replace("-", "").replace("_", "").replace(" ", "").lower()
        capabilities.add(_CAPABILITY_ALIASES.get(key, str(item).strip().lower()))

    lowered = (model_name or "").lower()
    for needles, capability in _NAME_HINTS:
        if any(needle in lowered for needle in needles):
            capabilities.add(capability)

    if not capabilities:
        capabilities.add("chat")
    return sorted(capabilities)


def _project_name(endpoint: str) -> str | None:
    # https://<account>.services.ai.azure.com/api/projects/<project>
    parts = [segment for segment in urlsplit(endpoint).path.split("/") if segment]
    if "projects" in parts:
        index = parts.index("projects")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present attribute or mapping key - SDK shapes vary."""
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def _fetch_from_foundry(endpoint: str) -> list[dict[str, Any]]:
    """Blocking call into the Foundry data plane. Run this off the event loop."""
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "azure-ai-projects and azure-identity are required for Foundry sync "
            "(pip install -r requirements-foundry.txt)"
        ) from exc

    discovered: list[dict[str, Any]] = []
    project = _project_name(endpoint)
    with DefaultAzureCredential() as credential, AIProjectClient(
        endpoint=endpoint, credential=credential
    ) as client:
        for deployment in client.deployments.list():
            name = _attr(deployment, "name", "deployment_name")
            if not name:
                continue
            model_name = _attr(deployment, "model_name", "model", default=name)
            discovered.append(
                {
                    "name": str(name),
                    "model_name": str(model_name),
                    "publisher": _attr(deployment, "model_publisher", "publisher"),
                    "model_version": _attr(deployment, "model_version", "version"),
                    "sku": _sku_name(_attr(deployment, "sku")),
                    "deployment_type": str(_attr(deployment, "type", "deployment_type", default="")) or None,
                    "endpoint": endpoint,
                    "project": project,
                    "raw_capabilities": _as_jsonable(_attr(deployment, "capabilities", default={})),
                }
            )
    return discovered


def _sku_name(sku: Any) -> str | None:
    if sku is None:
        return None
    if isinstance(sku, str):
        return sku
    return _attr(sku, "name", default=None)


def _as_jsonable(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return {str(item): "true" for item in value}
    return {}


def _fetch_from_file(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        if path.lower().endswith((".yaml", ".yml")):
            import yaml

            data = yaml.safe_load(handle) or []
        else:
            data = json.load(handle)

    if isinstance(data, dict):
        data = data.get("deployments", [])
    if not isinstance(data, list):
        raise RuntimeError("catalog file must contain a list of deployments")

    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        endpoint = item.get("endpoint") or get_settings().foundry_endpoint or "file://catalog"
        entries.append(
            {
                "name": str(item["name"]),
                "model_name": str(item.get("model_name") or item["name"]),
                "publisher": item.get("publisher"),
                "model_version": item.get("model_version"),
                "sku": item.get("sku"),
                "deployment_type": item.get("deployment_type"),
                "endpoint": endpoint,
                "project": item.get("project") or _project_name(endpoint),
                "raw_capabilities": _as_jsonable(item.get("capabilities", {})),
            }
        )
    return entries


# --------------------------------------------------------------------------- #
# Catalog store
# --------------------------------------------------------------------------- #
def _to_view(row: Any) -> ModelDeploymentView:
    return ModelDeploymentView(
        name=row["name"],
        model_name=row["model_name"],
        publisher=row["publisher"],
        model_version=row["model_version"],
        sku=row["sku"],
        deployment_type=row["deployment_type"],
        endpoint=row["endpoint"],
        project=row["project"],
        capabilities=db.loads(row["capabilities"], []),
        status=row["status"],
        source=row["source"],
        first_seen_at=row["first_seen_at"],
        synced_at=row["synced_at"],
    )


def list_deployments(status: ModelStatus | None = None) -> list[ModelDeploymentView]:
    if status is not None:
        rows = db.query(
            "SELECT * FROM model_deployments WHERE status = ? ORDER BY name COLLATE NOCASE",
            (status.value,),
        )
    else:
        rows = db.query("SELECT * FROM model_deployments ORDER BY name COLLATE NOCASE")
    return [_to_view(row) for row in rows]


def get(name: str) -> ModelDeploymentView | None:
    row = db.query_one("SELECT * FROM model_deployments WHERE name = ?", (name,))
    return _to_view(row) if row else None


def _persist(entries: list[dict[str, Any]], source: str) -> tuple[int, int, int]:
    now = utcnow_iso()
    existing = {row["name"]: row for row in db.query("SELECT * FROM model_deployments")}
    added = updated = 0

    with db.transaction() as conn:
        for entry in entries:
            capabilities = _normalise_capabilities(entry["raw_capabilities"], entry["model_name"])
            first_seen = existing[entry["name"]]["first_seen_at"] if entry["name"] in existing else now
            conn.execute(
                """
                INSERT INTO model_deployments (name, model_name, publisher, model_version, sku,
                                               deployment_type, endpoint, project, capabilities,
                                               raw_capabilities, status, source, first_seen_at, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    model_name = excluded.model_name,
                    publisher = excluded.publisher,
                    model_version = excluded.model_version,
                    sku = excluded.sku,
                    deployment_type = excluded.deployment_type,
                    endpoint = excluded.endpoint,
                    project = excluded.project,
                    capabilities = excluded.capabilities,
                    raw_capabilities = excluded.raw_capabilities,
                    status = excluded.status,
                    source = excluded.source,
                    synced_at = excluded.synced_at
                """,
                (
                    entry["name"],
                    entry["model_name"],
                    entry["publisher"],
                    entry["model_version"],
                    entry["sku"],
                    entry["deployment_type"],
                    entry["endpoint"],
                    entry["project"],
                    db.dumps(capabilities),
                    db.dumps(entry["raw_capabilities"]),
                    ModelStatus.AVAILABLE.value,
                    source,
                    first_seen,
                    now,
                ),
            )
            if entry["name"] in existing:
                updated += 1
            else:
                added += 1

        # Deployments that vanished from Foundry are retired rather than deleted
        # so historical tasks keep referring to a known model.
        seen = {entry["name"] for entry in entries}
        retired_names = [name for name in existing if name not in seen]
        for name in retired_names:
            conn.execute(
                "UPDATE model_deployments SET status = ?, synced_at = ? WHERE name = ?",
                (ModelStatus.UNAVAILABLE.value, now, name),
            )

    return added, updated, len(retired_names)


def sync() -> ModelSyncResult:
    """Pull the deployment list from Foundry into the local catalog."""
    settings = get_settings()

    if settings.foundry_endpoint:
        try:
            entries = _fetch_from_foundry(settings.foundry_endpoint)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            log.warning("Foundry sync failed: %s", exc)
            return ModelSyncResult(
                status="error",
                source="foundry",
                endpoint=settings.foundry_endpoint,
                detail=str(exc)[:500],
            )
        added, updated, retired = _persist(entries, "foundry")
        log.info(
            "Foundry sync: %d deployment(s) (+%d new, %d retired)", len(entries), added, retired
        )
        return ModelSyncResult(
            status="synced",
            source="foundry",
            endpoint=settings.foundry_endpoint,
            discovered=len(entries),
            added=added,
            updated=updated,
            retired=retired,
        )

    if settings.foundry_catalog_file:
        if not os.path.isfile(settings.foundry_catalog_file):
            return ModelSyncResult(
                status="error",
                source="catalog-file",
                detail=f"catalog file not found: {settings.foundry_catalog_file}",
            )
        try:
            entries = _fetch_from_file(settings.foundry_catalog_file)
        except Exception as exc:  # noqa: BLE001
            return ModelSyncResult(status="error", source="catalog-file", detail=str(exc)[:500])
        added, updated, retired = _persist(entries, "catalog-file")
        return ModelSyncResult(
            status="synced",
            source="catalog-file",
            endpoint=settings.foundry_catalog_file,
            discovered=len(entries),
            added=added,
            updated=updated,
            retired=retired,
        )

    return ModelSyncResult(
        status="disabled",
        source="none",
        detail="Set FOUNDRY_PROJECT_ENDPOINT or ORCH_FOUNDRY_CATALOG_FILE to enable the model catalog.",
    )


async def sync_async() -> ModelSyncResult:
    return await asyncio.to_thread(sync)


async def run_forever() -> None:
    """Periodically re-sync so Foundry stays the source of truth."""
    settings = get_settings()
    if not settings.foundry_enabled:
        log.info("Foundry model sync disabled (no endpoint or catalog file configured)")
        return
    while True:
        try:
            await sync_async()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("Foundry sync loop error")
        await asyncio.sleep(max(30, settings.foundry_sync_interval))
