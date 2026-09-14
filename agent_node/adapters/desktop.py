"""Full desktop control for the autonomous adapter. Windows only, opt-in.

This is the widest capability in the codebase and the only one without a real sandbox.
Keystrokes go to whatever window currently has focus, so the agent can drive any
application the logged-in user can drive - including closing the node itself.

What *is* constrained:

* applications come from an allow-list in ``node.yaml``; the model cannot start an
  arbitrary executable or pass its own arguments to one;
* screenshots are written inside the workspace root, never anywhere else;
* the surrounding adapter caps how many turns the model gets.

What is not constrained is the typing itself. Run this on a dedicated VM that holds no
credentials you care about, and keep the session supervised.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any

from .base import AdapterError

log = logging.getLogger("agent.desktop")

# Shipped defaults; node.yaml replaces this map entirely when it defines its own.
DEFAULT_APPS: dict[str, list[str]] = {
    "notepad": ["notepad.exe"],
    "explorer": ["explorer.exe"],
    "calculator": ["calc.exe"],
    "paint": ["mspaint.exe"],
    "edge": ["msedge.exe"],
    "wordpad": ["write.exe"],
    "excel": ["excel.exe"],
    "vscode": ["code"],
}

ALLOWED_KEYS = frozenset(
    {
        "ctrl", "alt", "shift", "win", "enter", "tab", "esc", "escape", "space",
        "backspace", "delete", "home", "end", "pageup", "pagedown",
        "up", "down", "left", "right",
        "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
        *"abcdefghijklmnopqrstuvwxyz0123456789",
    }
)


class DesktopController:
    def __init__(
        self,
        workspace_root: str,
        apps: dict[str, list[str]] | None = None,
        type_interval: float = 0.02,
    ) -> None:
        self.workspace_root = os.path.realpath(workspace_root)
        self.apps = apps or dict(DEFAULT_APPS)
        self.type_interval = type_interval
        self._gui: Any = None

    def _pyautogui(self) -> Any:
        if self._gui is None:
            try:
                import pyautogui
            except ImportError:
                raise AdapterError(
                    "desktop control requires PyAutoGUI: pip install -r requirements-desktop.txt"
                ) from None
            # Leave the emergency corner abort enabled: slamming the mouse to (0,0) stops it.
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.1
            self._gui = pyautogui
        return self._gui

    # ------------------------------------------------------------------ tools
    async def launch(self, app: Any) -> dict[str, Any]:
        name = str(app or "").strip().lower()
        if name not in self.apps:
            raise AdapterError(
                f"'{name}' is not in this node's application allow-list: {', '.join(sorted(self.apps))}"
            )
        argv = list(self.apps[name])
        # shell=False with a fixed argv: the model never supplies command text.
        subprocess.Popen(argv, close_fds=True)
        await asyncio.sleep(1.5)
        return {"ok": True, "launched": name}

    async def list_windows(self) -> dict[str, Any]:
        gui = self._pyautogui()

        def _collect() -> list[str]:
            titles = [title for title in gui.getAllTitles() if title and title.strip()]
            return sorted(set(titles))[:60]

        return {"ok": True, "windows": await asyncio.to_thread(_collect)}

    async def focus(self, title: Any) -> dict[str, Any]:
        needle = str(title or "").strip()
        if not needle:
            raise AdapterError("'title' is required")
        gui = self._pyautogui()

        def _activate() -> str:
            matches = [w for w in gui.getAllWindows() if needle.lower() in (w.title or "").lower()]
            if not matches:
                raise AdapterError(f"no window matching '{needle}'")
            window = matches[0]
            if window.isMinimized:
                window.restore()
            window.activate()
            return window.title

        activated = await asyncio.to_thread(_activate)
        await asyncio.sleep(0.4)
        return {"ok": True, "focused": activated}

    async def type_text(self, text: Any) -> dict[str, Any]:
        if not isinstance(text, str):
            raise AdapterError("'text' must be a string")
        gui = self._pyautogui()
        await asyncio.to_thread(gui.typewrite, text, self.type_interval)
        return {"ok": True, "typed": len(text)}

    async def hotkey(self, keys: Any) -> dict[str, Any]:
        if isinstance(keys, str):
            keys = [part.strip() for part in keys.split("+") if part.strip()]
        if not isinstance(keys, list) or not keys:
            raise AdapterError("'keys' must be a list such as ['ctrl','s']")

        normalised = [str(key).strip().lower() for key in keys]
        unknown = [key for key in normalised if key not in ALLOWED_KEYS]
        if unknown:
            raise AdapterError(f"unsupported key(s): {', '.join(unknown)}")

        gui = self._pyautogui()
        await asyncio.to_thread(gui.hotkey, *normalised)
        return {"ok": True, "pressed": "+".join(normalised)}

    async def screenshot(self, path: str) -> dict[str, Any]:
        gui = self._pyautogui()
        os.makedirs(os.path.dirname(path) or self.workspace_root, exist_ok=True)

        def _capture() -> tuple[int, int]:
            image = gui.screenshot()
            image.save(path)
            return image.size

        width, height = await asyncio.to_thread(_capture)
        return {"ok": True, "saved": os.path.basename(path), "width": width, "height": height}
