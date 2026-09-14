"""Microsoft Scout bridge."""

from __future__ import annotations

from .framework import FrameworkBridge


class ScoutAdapter(FrameworkBridge):
    name = "scout"
    namespaces = ("research", "browser", "web", "summarize", "plan", "data", "report")
    default_capabilities = (
        "research",
        "browser.automation",
        "llm.reasoning",
        "data.analysis",
        "reporting",
    )
