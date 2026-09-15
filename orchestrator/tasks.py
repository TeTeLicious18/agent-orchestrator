"""Task lifecycle persistence and telemetry stream."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from . import db
from .config import get_settings
from .models import (
    TERMINAL_TASK_STATUSES,
    TaskEvent,
    TaskStatus,
    TaskSubmission,
    TaskView,
    WorkflowStep,
    utcnow_iso,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def to_view(row: sqlite3.Row) -> TaskView:
    return TaskView(
        task_id=row["id"],
        workflow_id=row["workflow_id"],
        step_id=row["step_id"],
        title=row["title"],
        action=row["action"],
        payload=db.loads(row["payload"], {}),
        required_capabilities=db.loads(row["required_capabilities"], []),
        preferred_framework=row["preferred_framework"],
        target_agent_id=row["target_agent_id"],
        label_selector=db.loads(row["label_selector"], {}),
        depends_on=db.loads(row["depends_on"], []),
        priority=row["priority"],
        status=row["status"],
        agent_id=row["agent_id"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        timeout_seconds=row["timeout_seconds"],
        progress=row["progress"],
        submitted_by=row["submitted_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result=db.loads(row["result"], None),
        error=row["error"],
        requested_model=row["requested_model"],
        required_model_capabilities=db.loads(row["required_model_capabilities"], []),
        model_deployment=row["model_deployment"],
        prompt_tokens=row["prompt_tokens"] or 0,
        completion_tokens=row["completion_tokens"] or 0,
    )


def _insert(
    *,
    task_id: str,
    workflow_id: str | None,
    step_id: str | None,
    title: str,
    action: str,
    payload: dict[str, Any],
    required_capabilities: list[str],
    preferred_framework: str | None,
    target_agent_id: str | None,
    label_selector: dict[str, str],
    depends_on: list[str],
    priority: int,
    max_attempts: int,
    timeout_seconds: int,
    status: TaskStatus,
    submitted_by: str,
    requested_model: str | None,
    required_model_capabilities: list[str],
    conn: sqlite3.Connection,
) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO tasks (id, workflow_id, step_id, title, action, payload, required_capabilities,
                           preferred_framework, target_agent_id, label_selector, depends_on, priority,
                           status, attempts, max_attempts, timeout_seconds, submitted_by,
                           created_at, updated_at, progress, requested_model, required_model_capabilities)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            task_id,
            workflow_id,
            step_id,
            title,
            action,
            db.dumps(payload),
            db.dumps(required_capabilities),
            preferred_framework,
            target_agent_id,
            db.dumps(label_selector),
            db.dumps(depends_on),
            priority,
            status.value,
            max_attempts,
            timeout_seconds,
            submitted_by,
            now,
            now,
            requested_model,
            db.dumps(required_model_capabilities),
        ),
    )


def create(submission: TaskSubmission, submitted_by: str) -> TaskView:
    settings = get_settings()
    task_id = new_id("task")
    with db.transaction() as conn:
        _insert(
            task_id=task_id,
            workflow_id=None,
            step_id=None,
            title=submission.title,
            action=submission.action,
            payload=submission.payload,
            required_capabilities=submission.required_capabilities,
            preferred_framework=submission.preferred_framework.value
            if submission.preferred_framework
            else None,
            target_agent_id=submission.target_agent_id,
            label_selector=submission.label_selector,
            depends_on=[],
            priority=submission.priority,
            max_attempts=submission.max_attempts,
            timeout_seconds=submission.timeout_seconds or settings.default_task_timeout,
            status=TaskStatus.PENDING,
            submitted_by=submitted_by,
            requested_model=submission.requested_model,
            required_model_capabilities=submission.required_model_capabilities,
            conn=conn,
        )
        _append_event(conn, task_id, "submitted", f"Task accepted from {submitted_by}")
    return get(task_id)  # type: ignore[return-value]


def create_workflow_step(
    *,
    workflow_id: str,
    step: WorkflowStep,
    step_task_ids: dict[str, str],
    submitted_by: str,
    conn: sqlite3.Connection,
) -> str:
    settings = get_settings()
    task_id = new_id("task")
    status = TaskStatus.BLOCKED if step.depends_on else TaskStatus.PENDING
    _insert(
        task_id=task_id,
        workflow_id=workflow_id,
        step_id=step.step_id,
        title=step.title,
        action=step.action,
        payload=step.payload,
        required_capabilities=step.required_capabilities,
        preferred_framework=step.preferred_framework.value if step.preferred_framework else None,
        target_agent_id=step.target_agent_id,
        label_selector=step.label_selector,
        depends_on=step.depends_on,
        priority=step.priority,
        max_attempts=step.max_attempts,
        timeout_seconds=step.timeout_seconds or settings.default_task_timeout,
        status=status,
        submitted_by=submitted_by,
        requested_model=step.requested_model,
        required_model_capabilities=step.required_model_capabilities,
        conn=conn,
    )
    step_task_ids[step.step_id] = task_id
    _append_event(conn, task_id, "submitted", f"Workflow step '{step.step_id}' queued")
    return task_id


def get(task_id: str) -> TaskView | None:
    row = db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return to_view(row) if row else None


def list_tasks(
    status: TaskStatus | None = None,
    agent_id: str | None = None,
    workflow_id: str | None = None,
    limit: int = 100,
) -> list[TaskView]:
    limit = max(1, min(limit, 500))
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if workflow_id is not None:
        clauses.append("workflow_id = ?")
        params.append(workflow_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = db.query(f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ?", tuple(params))  # noqa: S608 - clauses are literals; filters are bound
    return [to_view(row) for row in rows]


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, ts, kind, message, data) VALUES (?, ?, ?, ?, ?)",
        (task_id, utcnow_iso(), kind, message[:2000], db.dumps(data or {})),
    )


def append_event(task_id: str, kind: str, message: str, data: dict[str, Any] | None = None) -> None:
    with db.transaction() as conn:
        _append_event(conn, task_id, kind, message, data)


def events(task_id: str, limit: int = 200) -> list[TaskEvent]:
    limit = max(1, min(limit, 1000))
    rows = db.query(
        "SELECT ts, kind, message, data FROM task_events WHERE task_id = ? ORDER BY id ASC LIMIT ?",
        (task_id, limit),
    )
    return [
        TaskEvent(ts=row["ts"], kind=row["kind"], message=row["message"], data=db.loads(row["data"], {}))
        for row in rows
    ]


def report_progress(task_id: str, agent_id: str, progress: int | None, message: str, telemetry: dict[str, Any]) -> bool:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT status, agent_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["agent_id"] != agent_id:
            return False
        current = TaskStatus(row["status"])
        if current in TERMINAL_TASK_STATUSES:
            return False

        now = utcnow_iso()
        # The first heartbeat from a running agent promotes the task out of the
        # "assigned" handshake state.
        status = TaskStatus.RUNNING if current is TaskStatus.ASSIGNED else current
        if progress is not None:
            conn.execute(
                "UPDATE tasks SET progress = ?, status = ?, updated_at = ? WHERE id = ?",
                (progress, status.value, now, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, task_id),
            )
        _append_event(conn, task_id, "progress", message or "progress update", telemetry)
    return True


def cancel(task_id: str, actor: str) -> TaskView | None:
    with db.transaction() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or TaskStatus(row["status"]) in TERMINAL_TASK_STATUSES:
            return None
        now = utcnow_iso()
        conn.execute(
            "UPDATE tasks SET status = ?, finished_at = ?, updated_at = ?, error = ? WHERE id = ?",
            (TaskStatus.CANCELLED.value, now, now, f"cancelled by {actor}", task_id),
        )
        _append_event(conn, task_id, "cancelled", f"Cancelled by {actor}")
    return get(task_id)
