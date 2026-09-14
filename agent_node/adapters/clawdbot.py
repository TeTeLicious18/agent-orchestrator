"""Clawdbot bridge."""

from __future__ import annotations

from .framework import FrameworkBridge


class ClawdbotAdapter(FrameworkBridge):
    name = "clawdbot"
    namespaces = ("automation", "sysadmin", "admin", "script", "workflow", "ops")
    default_capabilities = (
        "system.admin",
        "shell.exec",
        "filesystem",
        "llm.reasoning",
    )
