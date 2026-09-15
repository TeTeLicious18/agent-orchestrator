"""Compile-and-run support for the autonomous adapter. Opt-in, and genuinely dangerous.

Everything else in this package constrains *what* the model can do. This module does the
opposite: it lets the model write a source file and then execute it. Whatever the runtime
can reach, the generated program can reach - the workspace sandbox does not apply to a
running process.

The guard rails that do exist:

* runtimes come from an allow-list in ``node.yaml``; the model picks a name, never a
  command line, and never supplies interpreter flags;
* the program file must resolve inside the workspace;
* execution is capped by a timeout and the output is truncated;
* ``shell=False``, so nothing in the arguments is re-interpreted by a shell.

Enable this only on a disposable VM.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from .base import AdapterError

MAX_OUTPUT = 20_000

DEFAULT_RUNTIMES: dict[str, list[str]] = {
    "python": [sys.executable],
    "node": ["node"],
    "dotnet": ["dotnet", "run", "--project"],
    "powershell": ["powershell", "-NoProfile", "-NonInteractive", "-File"],
}


class CodeRunner:
    def __init__(
        self,
        workspace_root: str,
        runtimes: dict[str, list[str]] | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.workspace_root = os.path.realpath(workspace_root)
        self.runtimes = runtimes or dict(DEFAULT_RUNTIMES)
        self.timeout_seconds = timeout_seconds

    async def run(self, runtime: Any, path: str, args: Any = None) -> dict[str, Any]:
        name = str(runtime or "").strip().lower()
        if name not in self.runtimes:
            raise AdapterError(
                f"'{name}' is not an allowed runtime on this node: {', '.join(sorted(self.runtimes))}"
            )
        if not os.path.exists(path):
            raise AdapterError("program file not found; write it first")

        extra = args or []
        if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
            raise AdapterError("'args' must be a list of strings")

        argv = [*self.runtimes[name], path, *extra]

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.workspace_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
        except TimeoutError:
            process.kill()
            raise AdapterError(f"program exceeded the {self.timeout_seconds:.0f}s limit") from None

        return {
            # A non-zero exit is a normal observation: the model is expected to read the
            # error and fix its own source file.
            "ok": process.returncode == 0,
            "runtime": name,
            "exit_code": process.returncode,
            "stdout": stdout.decode(errors="replace")[:MAX_OUTPUT],
            "stderr": stderr.decode(errors="replace")[:MAX_OUTPUT],
        }
