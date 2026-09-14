"""Multi-agent workflow orchestration (DAG of tasks across the fleet)."""

from __future__ import annotations

from typing import Any

from . import db, tasks
from .models import (
    TaskStatus,
    WorkflowStatus,
    WorkflowSubmission,
    WorkflowView,
    utcnow_iso,
)

FAILED_STATUSES = {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT}


def create(submission: WorkflowSubmission, submitted_by: str) -> WorkflowView:
    workflow_id = tasks.new_id("wf")
    now = utcnow_iso()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO workflows (id, name, status, spec, submitted_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                submission.name,
                WorkflowStatus.RUNNING.value,
                db.dumps(submission.model_dump(mode="json")),
                submitted_by,
                now,
                now,
            ),
        )
        step_task_ids: dict[str, str] = {}
        for step in submission.steps:
            tasks.create_workflow_step(
                workflow_id=workflow_id,
                step=step,
                step_task_ids=step_task_ids,
                submitted_by=submitted_by,
                conn=conn,
            )
    return get(workflow_id)  # type: ignore[return-value]


def get(workflow_id: str) -> WorkflowView | None:
    row = db.query_one("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
    if row is None:
        return None
    step_rows = db.query(
        "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC", (workflow_id,)
    )
    return WorkflowView(
        workflow_id=row["id"],
        name=row["name"],
        status=row["status"],
        submitted_by=row["submitted_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        tasks=[tasks.to_view(step) for step in step_rows],
        result=db.loads(row["result"], None),
    )


def list_workflows(status: WorkflowStatus | None = None, limit: int = 100) -> list[WorkflowView]:
    limit = max(1, min(limit, 500))
    if status is not None:
        rows = db.query(
            "SELECT id FROM workflows WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status.value, limit),
        )
    else:
        rows = db.query("SELECT id FROM workflows ORDER BY created_at DESC LIMIT ?", (limit,))
    return [view for view in (get(row["id"]) for row in rows) if view is not None]


def advance(workflow_id: str) -> None:
    """Unblock ready steps, cancel unreachable ones and refresh workflow state."""
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, step_id, status, depends_on FROM tasks WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        if not rows:
            return

        by_step = {row["step_id"]: row for row in rows}
        now = utcnow_iso()

        for row in rows:
            if TaskStatus(row["status"]) is not TaskStatus.BLOCKED:
                continue
            deps = db.loads(row["depends_on"], [])
            dep_statuses = [
                TaskStatus(by_step[dep]["status"]) for dep in deps if dep in by_step
            ]
            if any(status in FAILED_STATUSES for status in dep_statuses):
                conn.execute(
                    "UPDATE tasks SET status = ?, error = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                    (
                        TaskStatus.CANCELLED.value,
                        "upstream step did not succeed",
                        now,
                        now,
                        row["id"],
                    ),
                )
                tasks._append_event(conn, row["id"], "cancelled", "Upstream dependency failed")
            elif dep_statuses and all(status is TaskStatus.SUCCEEDED for status in dep_statuses):
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.PENDING.value, now, row["id"]),
                )
                tasks._append_event(conn, row["id"], "unblocked", "Dependencies satisfied; queued for routing")

        refreshed = conn.execute(
            "SELECT step_id, status, title, agent_id, result, error FROM tasks WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        statuses = [TaskStatus(row["status"]) for row in refreshed]

        if any(status in FAILED_STATUSES for status in statuses) and all(
            status in FAILED_STATUSES or status is TaskStatus.SUCCEEDED for status in statuses
        ):
            workflow_status = WorkflowStatus.FAILED
        elif all(status is TaskStatus.SUCCEEDED for status in statuses):
            workflow_status = WorkflowStatus.SUCCEEDED
        else:
            workflow_status = WorkflowStatus.RUNNING

        result: dict[str, Any] | None = None
        if workflow_status is not WorkflowStatus.RUNNING:
            result = {
                "summary": _summarise(statuses),
                "steps": {
                    row["step_id"]: {
                        "title": row["title"],
                        "status": row["status"],
                        "agent_id": row["agent_id"],
                        "result": db.loads(row["result"], None),
                        "error": row["error"],
                    }
                    for row in refreshed
                },
            }

        conn.execute(
            "UPDATE workflows SET status = ?, updated_at = ?, result = ? WHERE id = ?",
            (
                workflow_status.value,
                now,
                db.dumps(result) if result is not None else None,
                workflow_id,
            ),
        )


def _summarise(statuses: list[TaskStatus]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for status in statuses:
        summary[status.value] = summary.get(status.value, 0) + 1
    return summary


def cancel(workflow_id: str, actor: str) -> WorkflowView | None:
    row = db.query_one("SELECT status FROM workflows WHERE id = ?", (workflow_id,))
    if row is None or WorkflowStatus(row["status"]) is not WorkflowStatus.RUNNING:
        return None
    now = utcnow_iso()
    with db.transaction() as conn:
        open_tasks = conn.execute(
            "SELECT id FROM tasks WHERE workflow_id = ? AND status NOT IN (?, ?, ?, ?)",
            (
                workflow_id,
                TaskStatus.SUCCEEDED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
                TaskStatus.TIMED_OUT.value,
            ),
        ).fetchall()
        for task_row in open_tasks:
            conn.execute(
                "UPDATE tasks SET status = ?, error = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.CANCELLED.value, f"workflow cancelled by {actor}", now, now, task_row["id"]),
            )
            tasks._append_event(conn, task_row["id"], "cancelled", f"Workflow cancelled by {actor}")
        conn.execute(
            "UPDATE workflows SET status = ?, updated_at = ? WHERE id = ?",
            (WorkflowStatus.CANCELLED.value, now, workflow_id),
        )
    return get(workflow_id)
