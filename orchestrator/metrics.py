"""Fleet-wide aggregation for the monitoring dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db
from .models import AgentStatus, FleetSummary, ModelStatus


def _counts(sql: str, params: tuple = ()) -> dict[str, int]:
    return {row[0]: row[1] for row in db.query(sql, params)}


def fleet_summary() -> FleetSummary:
    agents_total = db.query_one("SELECT COUNT(*) AS n FROM agents")
    online = db.query_one(
        "SELECT COUNT(*) AS n FROM agents WHERE status = ?", (AgentStatus.ONLINE.value,)
    )

    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    recent = db.query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE created_at >= ?", (one_hour_ago,)
    )

    finished = db.query(
        """
        SELECT status, started_at, finished_at FROM tasks
        WHERE finished_at IS NOT NULL AND started_at IS NOT NULL
        ORDER BY finished_at DESC LIMIT 500
        """
    )
    durations: list[float] = []
    successes = 0
    for row in finished:
        try:
            started = datetime.fromisoformat(row["started_at"])
            ended = datetime.fromisoformat(row["finished_at"])
        except ValueError:
            continue
        durations.append(max(0.0, (ended - started).total_seconds()))
        if row["status"] == "succeeded":
            successes += 1

    models_total = db.query_one("SELECT COUNT(*) AS n FROM model_deployments")
    models_available = db.query_one(
        "SELECT COUNT(*) AS n FROM model_deployments WHERE status = ?", (ModelStatus.AVAILABLE.value,)
    )
    token_rows = db.query(
        """
        SELECT model_deployment, SUM(prompt_tokens + completion_tokens) AS tokens
        FROM tasks WHERE model_deployment IS NOT NULL
        GROUP BY model_deployment
        """
    )
    tokens_by_model = {row["model_deployment"]: int(row["tokens"] or 0) for row in token_rows}

    return FleetSummary(
        agents_total=agents_total["n"] if agents_total else 0,
        agents_online=online["n"] if online else 0,
        agents_by_framework=_counts("SELECT framework, COUNT(*) FROM agents GROUP BY framework"),
        agents_by_platform=_counts("SELECT platform, COUNT(*) FROM agents GROUP BY platform"),
        tasks_by_status=_counts("SELECT status, COUNT(*) FROM tasks GROUP BY status"),
        workflows_by_status=_counts("SELECT status, COUNT(*) FROM workflows GROUP BY status"),
        tasks_last_hour=recent["n"] if recent else 0,
        avg_duration_seconds=round(sum(durations) / len(durations), 2) if durations else None,
        success_rate=round(successes / len(finished), 4) if finished else None,
        models_total=models_total["n"] if models_total else 0,
        models_available=models_available["n"] if models_available else 0,
        tokens_by_model=tokens_by_model,
        tokens_total=sum(tokens_by_model.values()),
    )
