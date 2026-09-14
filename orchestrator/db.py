"""SQLite persistence layer for the orchestrator.

A single embedded database keeps the hackathon deployment self-contained. Every
statement is parameterised; JSON blobs are serialised through helpers so that no
caller ever interpolates values into SQL.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    framework         TEXT NOT NULL,
    platform          TEXT NOT NULL,
    host              TEXT NOT NULL,
    capabilities      TEXT NOT NULL DEFAULT '[]',
    labels            TEXT NOT NULL DEFAULT '{}',
    max_concurrency   INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'offline',
    secret            TEXT NOT NULL,
    version           TEXT,
    registered_at     TEXT NOT NULL,
    last_heartbeat    TEXT
);

CREATE TABLE IF NOT EXISTS workflows (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL,
    spec          TEXT NOT NULL,
    submitted_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    result        TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id                    TEXT PRIMARY KEY,
    workflow_id           TEXT,
    step_id               TEXT,
    title                 TEXT NOT NULL,
    action                TEXT NOT NULL,
    payload               TEXT NOT NULL DEFAULT '{}',
    required_capabilities TEXT NOT NULL DEFAULT '[]',
    preferred_framework   TEXT,
    target_agent_id       TEXT,
    label_selector        TEXT NOT NULL DEFAULT '{}',
    depends_on            TEXT NOT NULL DEFAULT '[]',
    priority              INTEGER NOT NULL DEFAULT 5,
    status                TEXT NOT NULL,
    agent_id              TEXT,
    attempts              INTEGER NOT NULL DEFAULT 0,
    max_attempts          INTEGER NOT NULL DEFAULT 2,
    timeout_seconds       INTEGER NOT NULL,
    lease_expires_at      TEXT,
    submitted_by          TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    started_at            TEXT,
    finished_at           TEXT,
    progress              INTEGER NOT NULL DEFAULT 0,
    result                TEXT,
    error                 TEXT,
    required_model_capabilities TEXT NOT NULL DEFAULT '[]',
    requested_model       TEXT,
    model_deployment      TEXT,
    prompt_tokens         INTEGER NOT NULL DEFAULT 0,
    completion_tokens     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (workflow_id) REFERENCES workflows (id) ON DELETE CASCADE
);

-- Mirror of the model deployments published in the Microsoft Foundry project.
-- Foundry stays the source of truth; this table is a cache used for routing.
CREATE TABLE IF NOT EXISTS model_deployments (
    name             TEXT PRIMARY KEY,
    model_name       TEXT NOT NULL,
    publisher        TEXT,
    model_version    TEXT,
    sku              TEXT,
    deployment_type  TEXT,
    endpoint         TEXT NOT NULL,
    project          TEXT,
    capabilities     TEXT NOT NULL DEFAULT '[]',
    raw_capabilities TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'available',
    source           TEXT NOT NULL DEFAULT 'foundry',
    first_seen_at    TEXT NOT NULL,
    synced_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   TEXT NOT NULL,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    message   TEXT NOT NULL DEFAULT '',
    data      TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    outcome     TEXT NOT NULL DEFAULT 'ok',
    detail      TEXT NOT NULL DEFAULT '{}',
    source_ip   TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks (agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks (workflow_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events (task_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);
CREATE INDEX IF NOT EXISTS idx_models_status ON model_deployments (status);
"""

# Columns added after the first release. SQLite's CREATE TABLE IF NOT EXISTS
# will not add them to an existing database, so they are applied explicitly.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "tasks": {
        "required_model_capabilities": "TEXT NOT NULL DEFAULT '[]'",
        "requested_model": "TEXT",
        "model_deployment": "TEXT",
        "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
        "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
    },
}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    directory = os.path.dirname(os.path.abspath(settings.database_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
        _conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                # Table and column names come from the constant above, never input.
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def close_db() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Serialised write transaction. SQLite tolerates one writer at a time."""
    global _conn
    with _lock:
        if _conn is None:
            init_db()
        assert _conn is not None
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def query(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with _lock:
        if _conn is None:
            init_db()
        assert _conn is not None
        return list(_conn.execute(sql, params).fetchall())


def query_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def loads(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback
