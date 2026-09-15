"""Signed HTTP client used by an agent node to talk to the orchestrator."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

SIGNATURE_VERSION = "v1"


class OrchestratorClient:
    def __init__(self, base_url: str, agent_id: str, verify_tls: bool = True, timeout: float = 90.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._secret: str | None = None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            verify=verify_tls,
            # Dev tunnels answer anonymous requests with an HTML interstitial unless
            # this header is present. Ignored by every other host.
            headers={"X-Tunnel-Skip-AntiPhishing-Page": "true"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ auth
    @property
    def secret(self) -> str | None:
        return self._secret

    def _sign(self, method: str, path: str, body: bytes) -> dict[str, str]:
        if self._secret is None:
            raise RuntimeError("agent is not registered; no signing secret available")
        timestamp = datetime.now(UTC).isoformat()
        nonce = secrets.token_urlsafe(16)
        body_hash = hashlib.sha256(body).hexdigest()
        message = "\n".join([SIGNATURE_VERSION, method.upper(), path, timestamp, nonce, body_hash])
        digest = hmac.new(self._secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {
            "X-Agent-Id": self.agent_id,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": f"{SIGNATURE_VERSION}={digest}",
            "Content-Type": "application/json",
        }

    async def _signed(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode()
        # The server signs over the URL path only, so keep them identical.
        signed_path = urlsplit(self.base_url).path.rstrip("/") + path
        headers = self._sign(method, signed_path, body)
        response = await self._client.request(method, path, content=body, headers=headers)
        response.raise_for_status()
        return response.json() if response.content else None

    # ------------------------------------------------------------- lifecycle
    async def register(self, registration: dict[str, Any], bootstrap_token: str) -> dict[str, Any]:
        response = await self._client.post(
            "/api/v1/agents/register",
            json=registration,
            headers={"X-Bootstrap-Token": bootstrap_token},
        )
        response.raise_for_status()
        data = response.json()
        self._secret = data["agent_secret"]
        return data

    async def heartbeat(self, active_tasks: int, metrics: dict[str, float] | None = None) -> Any:
        return await self._signed(
            "POST",
            f"/api/v1/agents/{self.agent_id}/heartbeat",
            {"status": "online", "active_tasks": active_tasks, "metrics": metrics or {}},
        )

    async def lease(self, max_tasks: int, wait_seconds: int) -> list[dict[str, Any]]:
        return await self._signed(
            "POST",
            f"/api/v1/agents/{self.agent_id}/lease",
            {"max_tasks": max_tasks, "wait_seconds": wait_seconds},
        )

    async def progress(
        self, task_id: str, progress: int | None, message: str, telemetry: dict[str, Any] | None = None
    ) -> Any:
        return await self._signed(
            "POST",
            f"/api/v1/tasks/{task_id}/progress",
            {"progress": progress, "message": message, "telemetry": telemetry or {}},
        )

    async def complete(
        self,
        task_id: str,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
        usage: dict[str, int] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"status": status, "result": result, "error": error}
        if usage:
            payload["usage"] = usage
        return await self._signed("POST", f"/api/v1/tasks/{task_id}/complete", payload)
