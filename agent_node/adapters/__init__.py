"""Execution adapters that let one agent node front different AI frameworks."""

from .autonomous import AutonomousAdapter
from .base import Adapter, AdapterError, Emit
from .builtin import BuiltinAdapter
from .clawdbot import ClawdbotAdapter
from .scout import ScoutAdapter

__all__ = [
    "Adapter",
    "AdapterError",
    "Emit",
    "AutonomousAdapter",
    "BuiltinAdapter",
    "ClawdbotAdapter",
    "ScoutAdapter",
]
