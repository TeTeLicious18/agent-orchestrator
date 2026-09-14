"""Append-only audit trail for governance and compliance reviews."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from . import db
from .models import AuditEntry, utcnow_iso


def _client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def record(
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    outcome: str = "ok",
    detail: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (ts, actor, action, entity_type, entity_id, outcome, detail, source_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utcnow_iso(),
                actor,
                action,
                entity_type,
                entity_id,
                outcome,
                db.dumps(detail or {}),
                _client_ip(request),
            ),
        )


def recent(limit: int = 200, entity_id: str | None = None) -> list[AuditEntry]:
    limit = max(1, min(limit, 1000))
    if entity_id:
        rows = db.query(
            "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id DESC LIMIT ?",
            (entity_id, limit),
        )
    else:
        rows = db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    return [
        AuditEntry(
            ts=row["ts"],
            actor=row["actor"],
            action=row["action"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            outcome=row["outcome"],
            detail=db.loads(row["detail"], {}),
            source_ip=row["source_ip"],
        )
        for row in rows
    ]
