"""Agent node entry point.

Run one of these on every Azure VM or network PC that should join the fleet::

    python -m agent_node.runner --config agent_node/config.yaml

The node registers itself, advertises the capabilities its adapters provide,
long-polls the orchestrator for work and streams telemetry back while executing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import socket
import sys
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

from .adapters import AutonomousAdapter, BuiltinAdapter, ClawdbotAdapter, ScoutAdapter
from .adapters.base import AdapterError
from .client import OrchestratorClient
from .foundry import FoundryModelClient

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s")
log = logging.getLogger("agent")


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise SystemExit(f"{path} must contain a YAML mapping")
    return config


def build_adapters(config: dict[str, Any]) -> list[Any]:
    adapters: list[Any] = []
    framework_cfg = config.get("frameworks") or {}

    # Models live in Microsoft Foundry. When enabled, the node calls the
    # deployment the orchestrator bound to the task using its own identity.
    foundry_cfg = config.get("foundry") or {}
    foundry_client = (
        FoundryModelClient(
            enabled=True,
            max_output_tokens=int(foundry_cfg.get("max_output_tokens", 1024)),
            request_timeout=float(foundry_cfg.get("request_timeout", 120)),
        )
        if foundry_cfg.get("enabled", False)
        else None
    )
    model_aware = foundry_client is not None

    scout = framework_cfg.get("scout") or {}
    if scout.get("enabled", True):
        adapters.append(
            ScoutAdapter(
                endpoint=scout.get("endpoint"),
                api_key=os.getenv("SCOUT_API_KEY") or scout.get("api_key"),
                capabilities=scout.get("capabilities"),
                verify_tls=scout.get("verify_tls", True),
                model_aware=model_aware,
            )
        )

    clawdbot = framework_cfg.get("clawdbot") or {}
    if clawdbot.get("enabled", True):
        adapters.append(
            ClawdbotAdapter(
                endpoint=clawdbot.get("endpoint"),
                api_key=os.getenv("CLAWDBOT_API_KEY") or clawdbot.get("api_key"),
                capabilities=clawdbot.get("capabilities"),
                verify_tls=clawdbot.get("verify_tls", True),
                model_aware=model_aware,
            )
        )

    builtin = config.get("builtin") or {}
    if builtin.get("enabled", True):
        adapters.append(
            BuiltinAdapter(
                workspace_root=builtin.get("workspace_root", "agent_workspace"),
                allowed_commands=builtin.get("allowed_commands") or {},
                allow_http=builtin.get("allow_http", True),
                foundry_client=foundry_client,
            )
        )

    autonomous = config.get("autonomous") or {}
    if autonomous.get("enabled", False) and foundry_client is not None:
        workspace_root = autonomous.get("workspace_root") or builtin.get("workspace_root", "agent_workspace")

        browser_cfg = autonomous.get("browser") or {}
        browser = None
        if browser_cfg.get("enabled", False):
            from .adapters.browser import BrowserController

            browser = BrowserController(
                workspace_root=workspace_root,
                headless=browser_cfg.get("headless", False),
                channel=browser_cfg.get("channel", "msedge"),
                allowed_domains=browser_cfg.get("allowed_domains") or [],
            )

        desktop_cfg = autonomous.get("desktop_control") or {}
        desktop = None
        if desktop_cfg.get("enabled", False):
            from .adapters.desktop import DesktopController

            desktop = DesktopController(
                workspace_root=workspace_root,
                apps=desktop_cfg.get("apps") or None,
                type_interval=float(desktop_cfg.get("type_interval", 0.02)),
            )

        excel = None
        if (autonomous.get("excel") or {}).get("enabled", False):
            from .adapters.office import ExcelController

            excel = ExcelController()

        code_cfg = autonomous.get("code_execution") or {}
        code_runner = None
        if code_cfg.get("enabled", False):
            from .adapters.code_runner import CodeRunner

            code_runner = CodeRunner(
                workspace_root=workspace_root,
                runtimes=code_cfg.get("runtimes") or None,
                timeout_seconds=float(code_cfg.get("timeout_seconds", 60)),
            )

        adapters.append(
            AutonomousAdapter(
                workspace_root=workspace_root,
                foundry_client=foundry_client,
                allow_desktop=autonomous.get("allow_desktop", False),
                max_steps=int(autonomous.get("max_steps", 8)),
                browser=browser,
                desktop=desktop,
                excel=excel,
                code_runner=code_runner,
            )
        )
    return adapters


def _usage_of(result: Any) -> dict[str, int] | None:
    """Extract any token usage an adapter reported, for orchestrator telemetry."""
    if not isinstance(result, dict):
        return None
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
    }


class AgentNode:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.agent_id = config.get("agent_id") or f"agent-{socket.gethostname().lower()}"
        self.name = config.get("name") or socket.gethostname()
        self.framework = config.get("framework", "custom")
        self.node_platform = config.get("platform", "network_pc")
        self.labels = {str(k): str(v) for k, v in (config.get("labels") or {}).items()}
        self.max_concurrency = int(config.get("max_concurrency", 2))
        self.bootstrap_token = os.getenv("AGENT_BOOTSTRAP_TOKEN") or config.get("bootstrap_token", "")
        if not self.bootstrap_token:
            raise SystemExit(
                "No bootstrap token. Set AGENT_BOOTSTRAP_TOKEN (preferred) or 'bootstrap_token' in the config."
            )

        self.adapters = build_adapters(config)
        declared = set(config.get("extra_capabilities") or [])
        for adapter in self.adapters:
            declared.update(adapter.capabilities())
        self.capabilities = sorted(declared)

        self.client = OrchestratorClient(
            base_url=config.get("orchestrator_url", "http://localhost:8000"),
            agent_id=self.agent_id,
            verify_tls=config.get("verify_tls", True),
        )
        self._active: set[str] = set()
        self._slots = asyncio.Semaphore(self.max_concurrency)
        self._stopping = asyncio.Event()
        self.heartbeat_interval = 15

    # ------------------------------------------------------------ lifecycle
    async def register(self) -> None:
        registration = {
            "agent_id": self.agent_id,
            "name": self.name,
            "framework": self.framework,
            "platform": self.node_platform,
            "host": socket.gethostname(),
            "capabilities": self.capabilities,
            "labels": {**self.labels, "os": platform.system().lower()},
            "max_concurrency": self.max_concurrency,
            "version": platform.python_version(),
        }
        data = await self.client.register(registration, self.bootstrap_token)
        log.info(
            "registered as %s (%s) with capabilities: %s",
            self.agent_id,
            self.framework,
            ", ".join(self.capabilities) or "none",
        )
        self.heartbeat_interval = max(5, int(data.get("heartbeat_interval_seconds", 15)))

    async def heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.client.heartbeat(active_tasks=len(self._active))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 404):
                    log.warning("heartbeat rejected (%s); re-registering", exc.response.status_code)
                    await self.register()
                else:
                    log.warning("heartbeat failed: %s", exc)
            except httpx.HTTPError as exc:
                log.warning("heartbeat transport error: %s", exc)
            await asyncio.sleep(self.heartbeat_interval)

    async def lease_loop(self) -> None:
        while not self._stopping.is_set():
            await self._slots.acquire()
            self._slots.release()
            try:
                assignments = await self.client.lease(max_tasks=1, wait_seconds=20)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 404):
                    await self.register()
                    continue
                log.warning("lease failed: %s", exc)
                await asyncio.sleep(3)
                continue
            except httpx.HTTPError as exc:
                log.warning("lease transport error: %s", exc)
                await asyncio.sleep(3)
                continue

            for assignment in assignments or []:
                asyncio.create_task(self.run_task(assignment))

    # ------------------------------------------------------------ execution
    def select_adapter(self, action: str):
        for adapter in self.adapters:
            if adapter.handles(action):
                return adapter
        return None

    async def run_task(self, assignment: dict[str, Any]) -> None:
        task_id = assignment["task_id"]
        async with self._slots:
            self._active.add(task_id)
            log.info("executing %s (%s)", task_id, assignment["action"])

            async def emit(progress: int | None, message: str, telemetry: dict[str, Any]) -> None:
                try:
                    await self.client.progress(task_id, progress, message, telemetry)
                except httpx.HTTPError as exc:
                    log.debug("progress report dropped for %s: %s", task_id, exc)

            status, result, error = "failed", None, None
            try:
                adapter = self.select_adapter(assignment["action"])
                if adapter is None:
                    raise AdapterError(f"no adapter on this node handles '{assignment['action']}'")
                binding = assignment.get("model")
                await emit(
                    5,
                    f"accepted by {adapter.name} adapter"
                    + (f" using Foundry deployment '{binding['deployment_name']}'" if binding else ""),
                    {"host": socket.gethostname()},
                )
                result = await asyncio.wait_for(
                    adapter.execute(assignment, emit),
                    timeout=assignment.get("timeout_seconds", 900),
                )
                status = "succeeded"
            except TimeoutError:
                error = "local execution timeout"
            except AdapterError as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - report, never crash the node
                log.exception("task %s crashed", task_id)
                error = f"{type(exc).__name__}: {exc}"
            finally:
                self._active.discard(task_id)

            try:
                await self.client.complete(task_id, status, result, error, _usage_of(result))
                log.info("task %s -> %s", task_id, status)
            except httpx.HTTPError as exc:
                log.error("could not report completion for %s: %s", task_id, exc)

    async def run(self) -> None:
        await self.register()
        tasks = [
            asyncio.create_task(self.heartbeat_loop()),
            asyncio.create_task(self.lease_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._stopping.set()
            for task in tasks:
                task.cancel()
            await self.client.aclose()


async def main_async(config_path: str) -> None:
    node = AgentNode(load_config(config_path))
    await node.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Orchestrator agent node")
    parser.add_argument(
        "--config",
        default=os.getenv("AGENT_CONFIG", "agent_node/config.yaml"),
        help="path to the agent YAML configuration",
    )
    args = parser.parse_args()
    if not os.path.isfile(args.config):
        raise SystemExit(f"config file not found: {args.config}")
    try:
        asyncio.run(main_async(args.config))
    except KeyboardInterrupt:
        log.info("agent stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
