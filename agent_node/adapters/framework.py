"""Shared bridge for external agent frameworks reachable over HTTP.

A framework adapter forwards an orchestrator task to the local agent runtime
(Microsoft Scout, Clawdbot, ...) and streams status back. When no endpoint is
configured the adapter runs in ``simulation`` mode, which keeps the end-to-end
orchestration demo working on machines where the framework is not installed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .base import AdapterError, Emit


class FrameworkBridge:
    """Base class for framework-backed adapters."""

    name = "framework"
    namespaces: tuple[str, ...] = ()
    default_capabilities: tuple[str, ...] = ()

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        capabilities: list[str] | None = None,
        timeout: float = 600.0,
        verify_tls: bool = True,
        model_aware: bool = False,
    ) -> None:
        self.endpoint = endpoint.rstrip("/") if endpoint else None
        self.api_key = api_key
        self._capabilities = capabilities or list(self.default_capabilities)
        self.timeout = timeout
        self.verify_tls = verify_tls
        # When the node is wired to Foundry the framework runtime is told which
        # deployment to use instead of reading its own model configuration.
        self.model_aware = model_aware

    @property
    def simulated(self) -> bool:
        return self.endpoint is None

    def handles(self, action: str) -> bool:
        namespace = action.split(".", 1)[0].lower()
        return namespace in self.namespaces

    def capabilities(self) -> list[str]:
        caps = list(self._capabilities)
        if self.model_aware:
            caps.append("foundry.inference")
        return caps

    async def execute(self, task: dict[str, Any], emit: Emit) -> dict[str, Any]:
        if self.simulated:
            return await self._simulate(task, emit)
        return await self._invoke(task, emit)

    # ------------------------------------------------------------ execution
    async def _invoke(self, task: dict[str, Any], emit: Emit) -> dict[str, Any]:
        assert self.endpoint is not None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        binding = task.get("model")
        await emit(
            20,
            f"forwarding to {self.name} runtime",
            {
                "endpoint": self.endpoint,
                "deployment": binding["deployment_name"] if binding else None,
            },
        )
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify_tls) as client:
                response = await client.post(
                    f"{self.endpoint}/run",
                    json={
                        "task_id": task["task_id"],
                        "action": task["action"],
                        "title": task.get("title"),
                        "input": task.get("payload") or {},
                        # Foundry stays the source of truth for models: the
                        # framework is told which deployment to call.
                        "model": binding,
                    },
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise AdapterError(f"{self.name} runtime unreachable: {exc}") from None

        if response.status_code >= 400:
            raise AdapterError(
                f"{self.name} runtime returned {response.status_code}: {response.text[:500]}"
            )

        await emit(90, f"{self.name} runtime completed", {})
        try:
            body = response.json()
        except ValueError:
            body = {"output": response.text[:100_000]}
        result = {
            "framework": self.name,
            "mode": "live",
            "deployment": (task.get("model") or {}).get("deployment_name"),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "output": body,
        }
        # Lift token usage to the top level so the runner reports it to the orchestrator.
        if isinstance(body, dict) and isinstance(body.get("usage"), dict):
            result["usage"] = body["usage"]
        return result

    async def _simulate(self, task: dict[str, Any], emit: Emit) -> dict[str, Any]:
        """Deterministic stand-in used when the framework is not wired up."""
        binding = task.get("model") or {}
        steps = [
            (25, "interpreting the task"),
            (55, "executing framework skills"),
            (85, "validating output"),
        ]
        started = time.perf_counter()
        for percent, message in steps:
            await emit(percent, f"[simulated {self.name}] {message}", {})
            await asyncio.sleep(0.6)
        return {
            "framework": self.name,
            "mode": "simulated",
            "deployment": binding.get("deployment_name"),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "usage": {"prompt_tokens": 128, "completion_tokens": 64} if binding else None,
            "output": {
                "action": task["action"],
                "summary": (
                    f"Simulated {self.name} execution of '{task.get('title')}'"
                    + (f" using Foundry deployment '{binding['deployment_name']}'" if binding else "")
                    + ". Configure the framework endpoint in the agent config to run for real."
                ),
                "input": task.get("payload") or {},
            },
        }
