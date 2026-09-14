"""Runtime configuration for the orchestrator control plane."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("orchestrator.config")


def _csv(name: str) -> list[str]:
    raw = os.getenv(name, "") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_path: str = "data/orchestrator.db"
    api_keys: frozenset[str] = field(default_factory=frozenset)
    bootstrap_token: str = ""
    heartbeat_timeout: int = 45
    default_task_timeout: int = 900
    lease_seconds: int = 60
    max_clock_skew: int = 300
    cors_origins: tuple[str, ...] = ("*",)
    # --- Microsoft Foundry model catalog ---
    foundry_endpoint: str = ""
    foundry_catalog_file: str = ""
    foundry_sync_interval: int = 900
    model_preference: tuple[str, ...] = ()

    @property
    def foundry_enabled(self) -> bool:
        return bool(self.foundry_endpoint or self.foundry_catalog_file)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    api_keys = set(_csv("ORCH_API_KEYS"))
    if not api_keys:
        dev_key = f"dev-{secrets.token_urlsafe(24)}"
        api_keys.add(dev_key)
        log.warning(
            "ORCH_API_KEYS is not configured. Generated an ephemeral development "
            "key (also written to .dev-api-key): %s",
            dev_key,
        )
        try:
            with open(".dev-api-key", "w", encoding="utf-8") as handle:
                handle.write(dev_key)
        except OSError:  # pragma: no cover - best effort convenience only
            pass

    bootstrap = os.getenv("ORCH_BOOTSTRAP_TOKEN", "").strip()
    if not bootstrap:
        bootstrap = f"boot-{secrets.token_urlsafe(24)}"
        log.warning(
            "ORCH_BOOTSTRAP_TOKEN is not configured. Generated an ephemeral "
            "agent bootstrap token: %s",
            bootstrap,
        )

    origins = _csv("ORCH_CORS_ORIGINS") or ["*"]

    # FOUNDRY_PROJECT_ENDPOINT is the name the Foundry SDK samples use; the
    # ORCH_ prefixed variant is accepted so all service settings can share one
    # naming scheme.
    foundry_endpoint = (
        os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("ORCH_FOUNDRY_ENDPOINT") or ""
    ).strip()

    return Settings(
        database_path=os.getenv("ORCH_DB_PATH", "data/orchestrator.db"),
        api_keys=frozenset(api_keys),
        bootstrap_token=bootstrap,
        heartbeat_timeout=_int("ORCH_HEARTBEAT_TIMEOUT", 45),
        default_task_timeout=_int("ORCH_DEFAULT_TASK_TIMEOUT", 900),
        lease_seconds=_int("ORCH_LEASE_SECONDS", 60),
        max_clock_skew=_int("ORCH_MAX_CLOCK_SKEW", 300),
        cors_origins=tuple(origins),
        foundry_endpoint=foundry_endpoint,
        foundry_catalog_file=os.getenv("ORCH_FOUNDRY_CATALOG_FILE", "").strip(),
        foundry_sync_interval=_int("ORCH_FOUNDRY_SYNC_INTERVAL", 900),
        model_preference=tuple(_csv("ORCH_MODEL_PREFERENCE")),
    )
