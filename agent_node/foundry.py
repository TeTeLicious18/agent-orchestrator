"""Foundry inference client used by agent nodes.

The orchestrator never sends a model key. It sends the *coordinates* of a
deployment (endpoint, project, deployment name) and the node authenticates with
its own identity - a VM/PC managed identity in Azure, or the developer's
``az login`` session locally - through ``DefaultAzureCredential``.

If ``azure-ai-projects`` is not installed, or no credential is available, the
client falls back to a deterministic simulated completion so the orchestration
demo still runs end to end.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

log = logging.getLogger("agent.foundry")


class FoundryModelClient:
    def __init__(
        self,
        enabled: bool = True,
        max_output_tokens: int = 1024,
        request_timeout: float = 120.0,
    ) -> None:
        self.enabled = enabled
        self.max_output_tokens = max_output_tokens
        # Without this the SDK waits indefinitely, which surfaces as a task stuck in 'running'.
        self.request_timeout = request_timeout
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._sdk_available = self._probe_sdk() if enabled else False

    @staticmethod
    def _probe_sdk() -> bool:
        try:
            import azure.ai.projects  # noqa: F401
            import azure.identity  # noqa: F401
        except ImportError:
            log.info(
                "azure-ai-projects not installed; Foundry calls will run in simulation mode "
                "(pip install -r requirements-foundry.txt to go live)"
            )
            return False
        return True

    @property
    def live(self) -> bool:
        return self.enabled and self._sdk_available

    # ------------------------------------------------------------------ core
    def _openai_client(self, endpoint: str) -> Any:
        """Cache one authenticated client per Foundry endpoint."""
        with self._lock:
            client = self._clients.get(endpoint)
            if client is not None:
                return client

            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential

            project_client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
            client = project_client.get_openai_client()
            self._clients[endpoint] = client
            return client

    def _chat_sync(self, binding: dict[str, Any], messages: list[dict[str, str]]) -> dict[str, Any]:
        client = self._openai_client(binding["endpoint"])
        response = client.chat.completions.create(
            model=binding["deployment_name"],
            messages=messages,
            max_completion_tokens=self.max_output_tokens,
            timeout=self.request_timeout,
        )
        usage = getattr(response, "usage", None)
        choice = response.choices[0] if response.choices else None
        return {
            "mode": "live",
            "deployment": binding["deployment_name"],
            "model": binding.get("model_name"),
            "content": getattr(choice.message, "content", "") if choice else "",
            "finish_reason": getattr(choice, "finish_reason", None) if choice else None,
            "usage": {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            },
        }

    async def chat(self, binding: dict[str, Any], messages: list[dict[str, str]]) -> dict[str, Any]:
        if not self.live:
            return self._simulate(binding, messages)
        try:
            return await asyncio.to_thread(self._chat_sync, binding, messages)
        except Exception as exc:  # noqa: BLE001 - surfaced as a task error upstream
            raise RuntimeError(f"Foundry inference failed: {type(exc).__name__}: {exc}") from None

    # --------------------------------------------------------- tool calling
    def _chat_tools_sync(
        self,
        binding: dict[str, Any],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        client = self._openai_client(binding["endpoint"])
        response = client.chat.completions.create(
            model=binding["deployment_name"],
            messages=messages,
            tools=tools,
            max_completion_tokens=self.max_output_tokens,
            timeout=self.request_timeout,
        )
        usage = getattr(response, "usage", None)
        choice = response.choices[0] if response.choices else None
        message = getattr(choice, "message", None)
        calls = []
        for call in (getattr(message, "tool_calls", None) or []):
            calls.append(
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments or "{}",
                }
            )
        return {
            "content": getattr(message, "content", None),
            "tool_calls": calls,
            "finish_reason": getattr(choice, "finish_reason", None),
            "usage": {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            },
        }

    async def chat_with_tools(
        self,
        binding: dict[str, Any],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """One turn of a tool-calling conversation. The caller runs the loop."""
        if not self.live:
            simulated = self._simulate(binding, [m for m in messages if isinstance(m.get("content"), str)])
            return {
                "content": simulated["content"],
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": simulated["usage"],
            }
        try:
            return await asyncio.to_thread(self._chat_tools_sync, binding, messages, tools)
        except Exception as exc:  # noqa: BLE001 - surfaced as a task error upstream
            raise RuntimeError(f"Foundry tool call failed: {type(exc).__name__}: {exc}") from None

    # ------------------------------------------------------------ fallbacks
    @staticmethod
    def _simulate(binding: dict[str, Any], messages: list[dict[str, str]]) -> dict[str, Any]:
        prompt = " ".join(message.get("content", "") for message in messages)
        return {
            "mode": "simulated",
            "deployment": binding.get("deployment_name"),
            "model": binding.get("model_name"),
            "content": (
                f"[simulated {binding.get('model_name')}] "
                f"Responded to {len(prompt)} characters of prompt. Install "
                "requirements-foundry.txt and sign in to Azure to run this for real."
            ),
            "finish_reason": "stop",
            "usage": {
                "prompt_tokens": max(1, len(prompt) // 4),
                "completion_tokens": 32,
            },
        }
