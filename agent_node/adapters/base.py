"""Adapter contract shared by every execution backend."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

# Callback the runtime hands to an adapter so it can stream telemetry upstream.
Emit = Callable[[int | None, str, dict[str, Any]], Awaitable[None]]


class AdapterError(RuntimeError):
    """Raised when an adapter cannot complete a task."""


class Adapter(Protocol):
    name: str

    def handles(self, action: str) -> bool:
        """Return True when this adapter can execute the given action."""

    def capabilities(self) -> list[str]:
        """Capabilities advertised to the orchestrator during registration."""

    async def execute(self, task: dict[str, Any], emit: Emit) -> dict[str, Any]:
        """Run the task and return a JSON-serialisable result."""
