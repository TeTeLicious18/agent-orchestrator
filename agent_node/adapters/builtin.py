"""Built-in actions every node can perform: sandboxed files, HTTP and shell.

Every primitive here is deliberately restrictive:

* ``fs.*``  - confined to a configured workspace root, symlink escapes rejected.
* ``shell.exec`` - only pre-approved commands, executed without a shell so no
  argument can be interpreted as an operator.
* ``http.request`` - http/https only, with link-local and cloud metadata
  addresses blocked to prevent SSRF against the VM's instance metadata service.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import AdapterError, Emit

MAX_READ_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 2_000_000
BLOCKED_HOSTS = {"metadata.google.internal", "metadata.azure.com"}


class BuiltinAdapter:
    name = "builtin"

    def __init__(
        self,
        workspace_root: str,
        allowed_commands: dict[str, list[str]] | None = None,
        allow_http: bool = True,
        foundry_client: Any | None = None,
    ) -> None:
        self.workspace_root = os.path.realpath(workspace_root)
        os.makedirs(self.workspace_root, exist_ok=True)
        self.allowed_commands = allowed_commands or {}
        self.allow_http = allow_http
        self.foundry_client = foundry_client

    # ------------------------------------------------------------- contract
    def handles(self, action: str) -> bool:
        if action == "foundry.chat":
            return self.foundry_client is not None
        return action in {
            "echo",
            "fs.read",
            "fs.write",
            "fs.list",
            "http.request",
            "shell.exec",
        }

    def capabilities(self) -> list[str]:
        caps = ["filesystem"]
        if self.allow_http:
            caps.append("http")
        if self.allowed_commands:
            caps.append("shell.exec")
        if self.foundry_client is not None:
            caps.extend(["foundry.inference", "llm.reasoning"])
        return caps

    async def execute(self, task: dict[str, Any], emit: Emit) -> dict[str, Any]:
        action = task["action"]
        payload = task.get("payload") or {}
        await emit(10, f"builtin adapter starting '{action}'", {})

        handlers = {
            "echo": self._echo,
            "fs.read": self._fs_read,
            "fs.write": self._fs_write,
            "fs.list": self._fs_list,
            "http.request": self._http_request,
            "shell.exec": self._shell_exec,
            "foundry.chat": lambda payload, emit_fn: self._foundry_chat(task, payload, emit_fn),
        }
        handler = handlers.get(action)
        if handler is None:
            raise AdapterError(f"unsupported builtin action '{action}'")

        started = time.perf_counter()
        result = await handler(payload, emit)
        result["duration_seconds"] = round(time.perf_counter() - started, 3)
        await emit(100, f"builtin adapter finished '{action}'", {})
        return result

    # -------------------------------------------------------------- helpers
    def _resolve(self, relative_path: str) -> str:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise AdapterError("'path' is required")
        if os.path.isabs(relative_path) or relative_path.startswith("\\\\"):
            raise AdapterError("absolute paths are not permitted")
        candidate = os.path.realpath(os.path.join(self.workspace_root, relative_path))
        if os.path.commonpath([candidate, self.workspace_root]) != self.workspace_root:
            raise AdapterError("path escapes the agent workspace sandbox")
        return candidate

    # -------------------------------------------------------------- actions
    async def _echo(self, payload: dict[str, Any], emit: Emit) -> dict[str, Any]:
        return {"echo": payload}

    async def _foundry_chat(self, task: dict[str, Any], payload: dict[str, Any], emit: Emit) -> dict[str, Any]:
        binding = task.get("model")
        if not binding:
            raise AdapterError(
                "no Foundry deployment was bound to this task; submit it with "
                "'requested_model' or 'required_model_capabilities'"
            )

        messages = payload.get("messages")
        if messages is None:
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise AdapterError("provide either 'messages' or a non-empty 'prompt'")
            messages = []
            system = payload.get("system")
            if isinstance(system, str) and system.strip():
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

        if not isinstance(messages, list) or not all(
            isinstance(item, dict) and "role" in item and "content" in item for item in messages
        ):
            raise AdapterError("'messages' must be a list of {role, content} objects")

        await emit(
            45,
            f"calling Foundry deployment '{binding['deployment_name']}'",
            {"deployment": binding["deployment_name"], "project": binding.get("project")},
        )
        try:
            return await self.foundry_client.chat(binding, messages)
        except RuntimeError as exc:
            raise AdapterError(str(exc)) from None

    async def _fs_read(self, payload: dict[str, Any], emit: Emit) -> dict[str, Any]:
        path = self._resolve(payload.get("path", ""))
        if not os.path.isfile(path):
            raise AdapterError("file not found")
        if os.path.getsize(path) > MAX_READ_BYTES:
            raise AdapterError(f"file exceeds the {MAX_READ_BYTES} byte read limit")
        content = await asyncio.to_thread(
            lambda: open(path, "r", encoding="utf-8", errors="replace").read()
        )
        return {"path": os.path.relpath(path, self.workspace_root), "content": content}

    async def _fs_write(self, payload: dict[str, Any], emit: Emit) -> dict[str, Any]:
        path = self._resolve(payload.get("path", ""))
        content = payload.get("content", "")
        if not isinstance(content, str):
            raise AdapterError("'content' must be a string")
        if len(content.encode()) > MAX_READ_BYTES:
            raise AdapterError(f"content exceeds the {MAX_READ_BYTES} byte write limit")

        def _write() -> None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)

        await asyncio.to_thread(_write)
        return {"path": os.path.relpath(path, self.workspace_root), "bytes_written": len(content.encode())}

    async def _fs_list(self, payload: dict[str, Any], emit: Emit) -> dict[str, Any]:
        path = self._resolve(payload.get("path", "."))
        if not os.path.isdir(path):
            raise AdapterError("directory not found")
        entries = await asyncio.to_thread(lambda: sorted(os.listdir(path))[:500])
        return {"path": os.path.relpath(path, self.workspace_root), "entries": entries}

    async def _http_request(self, payload: dict[str, Any], emit: Emit) -> dict[str, Any]:
        if not self.allow_http:
            raise AdapterError("http actions are disabled on this agent")
        url = payload.get("url", "")
        method = str(payload.get("method", "GET")).upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise AdapterError(f"unsupported HTTP method '{method}'")
        _validate_url(url)

        await emit(40, f"{method} {url}", {})
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.request(
                method,
                url,
                json=payload.get("json"),
                headers=payload.get("headers") or {},
            )
        body = response.text[:MAX_RESPONSE_BYTES]
        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": body,
            "truncated": len(response.text) > len(body),
        }

    async def _shell_exec(self, payload: dict[str, Any], emit: Emit) -> dict[str, Any]:
        command = payload.get("command", "")
        if command not in self.allowed_commands:
            raise AdapterError(
                "command is not in this agent's allow-list; "
                f"permitted commands: {sorted(self.allowed_commands) or 'none'}"
            )
        argv = list(self.allowed_commands[command])
        extra = payload.get("args") or []
        if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
            raise AdapterError("'args' must be a list of strings")
        argv.extend(extra)

        await emit(40, f"executing allow-listed command '{command}'", {"argv": argv})
        # shell=False: arguments are passed verbatim, never re-parsed by a shell.
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.workspace_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=float(payload.get("timeout_seconds", 120))
            )
        except asyncio.TimeoutError:
            process.kill()
            raise AdapterError("command timed out") from None

        if process.returncode != 0:
            raise AdapterError(
                f"command exited with code {process.returncode}: {stderr.decode(errors='replace')[:500]}"
            )
        return {
            "command": command,
            "exit_code": process.returncode,
            "stdout": stdout.decode(errors="replace")[:100_000],
            "stderr": stderr.decode(errors="replace")[:10_000],
        }


def _validate_url(url: str) -> None:
    if not isinstance(url, str) or not url:
        raise AdapterError("'url' is required")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise AdapterError("only http and https URLs are allowed")
    host = parts.hostname
    if not host or host.lower() in BLOCKED_HOSTS:
        raise AdapterError("target host is blocked")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise AdapterError(f"could not resolve host: {exc}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_link_local or address.is_loopback or address.is_reserved or address.is_multicast:
            raise AdapterError("requests to link-local, loopback or reserved addresses are blocked")
