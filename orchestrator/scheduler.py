"""Dispatch loop: leasing work to agents, completions, timeouts and recovery."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from . import db, foundry, registry, tasks, workflows
from .config import get_settings
from .models import (
    ModelBinding,
    ModelDeploymentView,
    ModelStatus,
    TaskAssignment,
    TaskCompletion,
    TaskStatus,
    TaskView,
    utcnow_iso,
)
from .routing import rank_tasks_for_agent, requires_model, select_model

log = logging.getLogger("orchestrator.scheduler")

ACTIVE = (TaskStatus.ASSIGNED.value, TaskStatus.RUNNING.value)


def _now() -> datetime:
    return datetime.now(UTC)


def _to_binding(deployment: ModelDeploymentView) -> ModelBinding:
    """Only non-secret coordinates travel to the agent; auth is Entra ID there."""
    return ModelBinding(
        deployment_name=deployment.name,
        model_name=deployment.model_name,
        publisher=deployment.publisher,
        model_version=deployment.model_version,
        endpoint=deployment.endpoint,
        project=deployment.project,
        capabilities=deployment.capabilities,
    )


def lease(agent_id: str, max_tasks: int) -> list[TaskAssignment]:
    """Atomically hand pending work to a specific agent."""
    settings = get_settings()
    agent = registry.get(agent_id)
    if agent is None:
        return []

    headroom = agent.max_concurrency - agent.active_tasks
    if headroom <= 0:
        return []

    pending = tasks.list_tasks(status=TaskStatus.PENDING, limit=500)
    ranked = rank_tasks_for_agent(agent, pending)[: min(max_tasks, headroom)]
    if not ranked:
        return []

    catalog = foundry.list_deployments(status=ModelStatus.AVAILABLE)
    assignments: list[TaskAssignment] = []
    lease_expiry = (_now() + timedelta(seconds=settings.lease_seconds)).isoformat()
    now_iso = utcnow_iso()

    with db.transaction() as conn:
        for task in ranked:
            binding: ModelBinding | None = None
            if requires_model(task):
                decision = select_model(task, catalog)
                if decision.deployment is None:
                    # Leave the task pending - Foundry may publish a suitable
                    # deployment later. Record the reason once, not every poll.
                    last = conn.execute(
                        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                        (task.task_id,),
                    ).fetchone()
                    if last is None or last["kind"] != "model_pending":
                        tasks._append_event(
                            conn,
                            task.task_id,
                            "model_pending",
                            f"Waiting for a Foundry model: {decision.reason}",
                        )
                    continue
                binding = _to_binding(decision.deployment)

            # Guarded update: only claim rows still pending, so two agents racing
            # for the same task cannot both win.
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, agent_id = ?, attempts = attempts + 1,
                    lease_expires_at = ?, started_at = COALESCE(started_at, ?), updated_at = ?,
                    model_deployment = ?
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.ASSIGNED.value,
                    agent_id,
                    lease_expiry,
                    now_iso,
                    now_iso,
                    binding.deployment_name if binding else None,
                    task.task_id,
                    TaskStatus.PENDING.value,
                ),
            )
            if cursor.rowcount == 0:
                continue
            row = conn.execute("SELECT attempts FROM tasks WHERE id = ?", (task.task_id,)).fetchone()
            tasks._append_event(
                conn,
                task.task_id,
                "assigned",
                f"Dispatched to {agent.name} ({agent.agent_id}) on {agent.platform.value}",
                {"agent_id": agent.agent_id, "framework": agent.framework.value, "host": agent.host},
            )
            if binding is not None:
                tasks._append_event(
                    conn,
                    task.task_id,
                    "model_bound",
                    f"Bound to Foundry deployment '{binding.deployment_name}' ({binding.model_name})",
                    {
                        "deployment": binding.deployment_name,
                        "model": binding.model_name,
                        "project": binding.project,
                    },
                )
            assignments.append(
                TaskAssignment(
                    task_id=task.task_id,
                    workflow_id=task.workflow_id,
                    title=task.title,
                    action=task.action,
                    payload=task.payload,
                    timeout_seconds=task.timeout_seconds,
                    lease_expires_at=lease_expiry,
                    attempt=row["attempts"] if row else task.attempts + 1,
                    model=binding,
                )
            )
    return assignments


async def lease_with_wait(agent_id: str, max_tasks: int, wait_seconds: int) -> list[TaskAssignment]:
    """Long-poll wrapper so idle agents do not hammer the control plane."""
    deadline = _now() + timedelta(seconds=wait_seconds)
    while True:
        assignments = await asyncio.to_thread(lease, agent_id, max_tasks)
        if assignments or _now() >= deadline:
            return assignments
        await asyncio.sleep(1.0)


def renew_lease(agent_id: str) -> None:
    settings = get_settings()
    expiry = (_now() + timedelta(seconds=settings.lease_seconds)).isoformat()
    with db.transaction() as conn:
        conn.execute(
            f"UPDATE tasks SET lease_expires_at = ? WHERE agent_id = ? AND status IN ({','.join('?' * len(ACTIVE))})",  # noqa: S608 - placeholders only
            (expiry, agent_id, *ACTIVE),
        )


def complete(task_id: str, agent_id: str, completion: TaskCompletion) -> TaskView | None:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT status, agent_id, attempts, max_attempts, workflow_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["agent_id"] != agent_id:
            return None
        if TaskStatus(row["status"]) not in {TaskStatus.ASSIGNED, TaskStatus.RUNNING}:
            return None

        now = utcnow_iso()
        retryable = (
            completion.status is TaskStatus.FAILED and row["attempts"] < row["max_attempts"]
        )
        if retryable:
            conn.execute(
                """
                UPDATE tasks SET status = ?, agent_id = NULL, lease_expires_at = NULL,
                                 updated_at = ?, error = ?
                WHERE id = ?
                """,
                (TaskStatus.PENDING.value, now, completion.error, task_id),
            )
            tasks._append_event(
                conn,
                task_id,
                "retry",
                f"Attempt {row['attempts']} failed on {agent_id}; re-queued for another agent",
                {"error": completion.error},
            )
        else:
            final_progress = 100 if completion.status is TaskStatus.SUCCEEDED else 0
            conn.execute(
                """
                UPDATE tasks SET status = ?, progress = ?, result = ?, error = ?,
                                 finished_at = ?, updated_at = ?, lease_expires_at = NULL
                WHERE id = ?
                """,
                (
                    completion.status.value,
                    final_progress,
                    db.dumps(completion.result) if completion.result is not None else None,
                    completion.error,
                    now,
                    now,
                    task_id,
                ),
            )
            tasks._append_event(
                conn,
                task_id,
                completion.status.value,
                completion.error or "Task finished successfully",
                {"agent_id": agent_id},
            )

        if completion.usage is not None:
            conn.execute(
                """
                UPDATE tasks SET prompt_tokens = prompt_tokens + ?,
                                 completion_tokens = completion_tokens + ?
                WHERE id = ?
                """,
                (completion.usage.prompt_tokens, completion.usage.completion_tokens, task_id),
            )
            tasks._append_event(
                conn,
                task_id,
                "model_usage",
                (
                    f"{completion.usage.prompt_tokens} prompt + "
                    f"{completion.usage.completion_tokens} completion tokens"
                ),
                completion.usage.model_dump(),
            )
        workflow_id = row["workflow_id"]

    if workflow_id:
        workflows.advance(workflow_id)
    return tasks.get(task_id)


def reap() -> dict[str, int]:
    """Housekeeping pass: expire leases, enforce timeouts, park dead agents."""
    stats = {"expired_leases": 0, "timed_out": 0, "offline_agents": 0}
    now = _now()
    now_iso = now.isoformat()

    stale_agents = registry.expire_stale_agents()
    stats["offline_agents"] = len(stale_agents)

    touched_workflows: set[str] = set()

    with db.transaction() as conn:
        rows = conn.execute(
            f"""
            SELECT id, workflow_id, attempts, max_attempts, started_at, timeout_seconds,
                   lease_expires_at, agent_id
            FROM tasks WHERE status IN ({','.join('?' * len(ACTIVE))})
            """,  # noqa: S608 - placeholders only; statuses are bound
            ACTIVE,
        ).fetchall()

        for row in rows:
            hard_timeout = False
            if row["started_at"]:
                try:
                    started = datetime.fromisoformat(row["started_at"])
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=UTC)
                    hard_timeout = (now - started).total_seconds() > row["timeout_seconds"]
                except ValueError:
                    hard_timeout = False

            lease_lost = bool(row["lease_expires_at"]) and row["lease_expires_at"] < now_iso
            if not (hard_timeout or lease_lost):
                continue

            if hard_timeout or row["attempts"] >= row["max_attempts"]:
                conn.execute(
                    """
                    UPDATE tasks SET status = ?, error = ?, finished_at = ?, updated_at = ?,
                                     lease_expires_at = NULL
                    WHERE id = ?
                    """,
                    (
                        TaskStatus.TIMED_OUT.value,
                        "execution timeout exceeded" if hard_timeout else "agent lease expired",
                        now_iso,
                        now_iso,
                        row["id"],
                    ),
                )
                tasks._append_event(
                    conn,
                    row["id"],
                    "timed_out",
                    "Task timed out" if hard_timeout else "Agent stopped responding",
                    {"agent_id": row["agent_id"]},
                )
                stats["timed_out"] += 1
            else:
                conn.execute(
                    """
                    UPDATE tasks SET status = ?, agent_id = NULL, lease_expires_at = NULL,
                                     started_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (TaskStatus.PENDING.value, now_iso, row["id"]),
                )
                tasks._append_event(
                    conn,
                    row["id"],
                    "requeued",
                    f"Lease lost on {row['agent_id']}; task returned to the queue",
                )
                stats["expired_leases"] += 1

            if row["workflow_id"]:
                touched_workflows.add(row["workflow_id"])

    for workflow_id in touched_workflows:
        workflows.advance(workflow_id)
    return stats


async def run_forever(interval: float = 5.0) -> None:
    while True:
        try:
            stats = await asyncio.to_thread(reap)
            if any(stats.values()):
                log.info("scheduler housekeeping: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("scheduler housekeeping failed")
        await asyncio.sleep(interval)
