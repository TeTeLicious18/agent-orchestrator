"""Agent fleet registry: registration, liveness and inventory queries."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from . import db
from .config import get_settings
from .models import (
    AgentPlatform,
    AgentRegistration,
    AgentStatus,
    AgentView,
    Heartbeat,
    TaskStatus,
    utcnow_iso,
)
from .security import new_agent_secret

ACTIVE_STATUSES = (TaskStatus.ASSIGNED.value, TaskStatus.RUNNING.value)


def _active_task_counts() -> dict[str, int]:
    rows = db.query(
        f"""
        SELECT agent_id, COUNT(*) AS n FROM tasks
        WHERE agent_id IS NOT NULL AND status IN ({",".join("?" * len(ACTIVE_STATUSES))})
        GROUP BY agent_id
        """,  # noqa: S608 - interpolates placeholders only; every value is bound
        ACTIVE_STATUSES,
    )
    return {row["agent_id"]: row["n"] for row in rows}


def to_view(row: sqlite3.Row, active_tasks: int = 0) -> AgentView:
    return AgentView(
        agent_id=row["id"],
        name=row["name"],
        framework=row["framework"],
        platform=row["platform"],
        host=row["host"],
        capabilities=db.loads(row["capabilities"], []),
        labels=db.loads(row["labels"], {}),
        max_concurrency=row["max_concurrency"],
        status=row["status"],
        version=row["version"],
        registered_at=row["registered_at"],
        last_heartbeat=row["last_heartbeat"],
        active_tasks=active_tasks,
    )


def register(registration: AgentRegistration) -> tuple[AgentView, str]:
    """Create or re-provision an agent. Returns the view and its fresh secret."""
    secret = new_agent_secret()
    now = utcnow_iso()
    existing = db.query_one("SELECT registered_at FROM agents WHERE id = ?", (registration.agent_id,))
    registered_at = existing["registered_at"] if existing else now

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO agents (id, name, framework, platform, host, capabilities, labels,
                                max_concurrency, status, secret, version, registered_at, last_heartbeat)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                framework = excluded.framework,
                platform = excluded.platform,
                host = excluded.host,
                capabilities = excluded.capabilities,
                labels = excluded.labels,
                max_concurrency = excluded.max_concurrency,
                status = excluded.status,
                secret = excluded.secret,
                version = excluded.version,
                last_heartbeat = excluded.last_heartbeat
            """,
            (
                registration.agent_id,
                registration.name,
                registration.framework.value,
                registration.platform.value,
                registration.host,
                db.dumps(registration.capabilities),
                db.dumps(registration.labels),
                registration.max_concurrency,
                AgentStatus.ONLINE.value,
                secret,
                registration.version,
                registered_at,
                now,
            ),
        )

    row = db.query_one("SELECT * FROM agents WHERE id = ?", (registration.agent_id,))
    assert row is not None
    return to_view(row, _active_task_counts().get(registration.agent_id, 0)), secret


def heartbeat(agent_id: str, beat: Heartbeat) -> AgentView | None:
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE agents SET last_heartbeat = ?, status = ? WHERE id = ?",
            (utcnow_iso(), beat.status.value, agent_id),
        )
        if cursor.rowcount == 0:
            return None
    return get(agent_id)


def get(agent_id: str) -> AgentView | None:
    row = db.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if row is None:
        return None
    return to_view(row, _active_task_counts().get(agent_id, 0))


def list_agents(
    status: AgentStatus | None = None,
    framework: str | None = None,
    platform: AgentPlatform | None = None,
) -> list[AgentView]:
    clauses: list[str] = []
    params: list[str] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    if framework is not None:
        clauses.append("framework = ?")
        params.append(framework)
    if platform is not None:
        clauses.append("platform = ?")
        params.append(platform.value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query(f"SELECT * FROM agents {where} ORDER BY name COLLATE NOCASE", tuple(params))  # noqa: S608 - clauses are literals; filters are bound
    counts = _active_task_counts()
    return [to_view(row, counts.get(row["id"], 0)) for row in rows]


def deregister(agent_id: str) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        return cursor.rowcount > 0


def set_status(agent_id: str, status: AgentStatus) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute("UPDATE agents SET status = ? WHERE id = ?", (status.value, agent_id))
        return cursor.rowcount > 0


def expire_stale_agents() -> list[str]:
    """Flip agents that stopped heart-beating to ``offline``."""
    settings = get_settings()
    cutoff = (datetime.now(UTC) - timedelta(seconds=settings.heartbeat_timeout)).isoformat()
    rows = db.query(
        "SELECT id FROM agents WHERE status != ? AND (last_heartbeat IS NULL OR last_heartbeat < ?)",
        (AgentStatus.OFFLINE.value, cutoff),
    )
    stale = [row["id"] for row in rows]
    if stale:
        placeholders = ",".join("?" * len(stale))
        with db.transaction() as conn:
            conn.execute(
                f"UPDATE agents SET status = ? WHERE id IN ({placeholders})",  # noqa: S608 - placeholders only
                (AgentStatus.OFFLINE.value, *stale),
            )
    return stale
