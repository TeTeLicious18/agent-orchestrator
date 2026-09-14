"""FastAPI application exposing the orchestration control plane."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit, db, foundry, metrics, registry, scheduler, tasks, workflows
from .config import get_settings
from .models import (
    AgentPlatform,
    AgentRegistration,
    AgentStatus,
    AgentView,
    AuditEntry,
    FleetSummary,
    Heartbeat,
    LeaseRequest,
    ModelDeploymentView,
    ModelStatus,
    ModelSyncResult,
    TaskAssignment,
    TaskCompletion,
    TaskEvent,
    TaskProgress,
    TaskStatus,
    TaskSubmission,
    TaskView,
    WorkflowStatus,
    WorkflowSubmission,
    WorkflowView,
)
from .routing import select_agent, select_model
from .security import Principal, require_agent, require_bootstrap_token, require_operator

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s"
)
log = logging.getLogger("orchestrator")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class RegistrationResponse(BaseModel):
    agent: AgentView
    agent_secret: str
    signature_version: str = "v1"
    heartbeat_interval_seconds: int


class RoutingPreview(BaseModel):
    agent_id: str | None
    agent_name: str | None
    score: float
    reason: str
    model_deployment: str | None = None
    model_name: str | None = None
    model_reason: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    settings = get_settings()
    log.info("orchestrator starting - database=%s", settings.database_path)
    background: list = []
    if not os.getenv("ORCH_DISABLE_SCHEDULER"):
        import asyncio

        background.append(asyncio.create_task(scheduler.run_forever()))
        if settings.foundry_enabled:
            log.info(
                "Microsoft Foundry model sync enabled - source=%s",
                settings.foundry_endpoint or settings.foundry_catalog_file,
            )
            background.append(asyncio.create_task(foundry.run_forever()))
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        db.close_db()


app = FastAPI(
    title="Multiplatform Automated Agent Orchestrator",
    description=(
        "Control plane that registers, routes work to and monitors autonomous agents "
        "running on Azure VMs and network-connected PCs."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Health & dashboard
# --------------------------------------------------------------------------- #
@app.get("/healthz", tags=["system"])
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "agent-orchestrator"}


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard/")


# --------------------------------------------------------------------------- #
# Agent plane
# --------------------------------------------------------------------------- #
@app.post(
    "/api/v1/agents/register",
    response_model=RegistrationResponse,
    tags=["agents"],
    dependencies=[Depends(require_bootstrap_token)],
)
async def register_agent(registration: AgentRegistration, request: Request) -> RegistrationResponse:
    agent, secret = registry.register(registration)
    audit.record(
        actor=f"agent:{agent.agent_id}",
        action="agent.register",
        entity_type="agent",
        entity_id=agent.agent_id,
        detail={
            "framework": agent.framework.value,
            "platform": agent.platform.value,
            "host": agent.host,
            "capabilities": agent.capabilities,
        },
        request=request,
    )
    return RegistrationResponse(
        agent=agent,
        agent_secret=secret,
        heartbeat_interval_seconds=max(5, get_settings().heartbeat_timeout // 3),
    )


@app.post("/api/v1/agents/{agent_id}/heartbeat", response_model=AgentView, tags=["agents"])
async def agent_heartbeat(
    agent_id: str,
    beat: Heartbeat,
    principal: Principal = Depends(require_agent),
) -> AgentView:
    agent = registry.heartbeat(agent_id, beat)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    scheduler.renew_lease(agent_id)
    return agent


@app.post("/api/v1/agents/{agent_id}/lease", response_model=list[TaskAssignment], tags=["agents"])
async def lease_tasks(
    agent_id: str,
    lease_request: LeaseRequest,
    principal: Principal = Depends(require_agent),
) -> list[TaskAssignment]:
    agent = registry.get(agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    if agent.status is AgentStatus.DRAINING:
        return []
    return await scheduler.lease_with_wait(
        agent_id, lease_request.max_tasks, lease_request.wait_seconds
    )


@app.post("/api/v1/tasks/{task_id}/progress", tags=["agents"])
async def report_progress(
    task_id: str,
    progress: TaskProgress,
    principal: Principal = Depends(require_agent),
) -> dict[str, str]:
    ok = tasks.report_progress(
        task_id, principal.identifier, progress.progress, progress.message, progress.telemetry
    )
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, "task is not owned by this agent or already closed")
    return {"status": "accepted"}


@app.post("/api/v1/tasks/{task_id}/complete", response_model=TaskView, tags=["agents"])
async def complete_task(
    task_id: str,
    completion: TaskCompletion,
    request: Request,
    principal: Principal = Depends(require_agent),
) -> TaskView:
    task = scheduler.complete(task_id, principal.identifier, completion)
    if task is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "task is not owned by this agent or already closed")
    audit.record(
        actor=f"agent:{principal.identifier}",
        action="task.complete",
        entity_type="task",
        entity_id=task_id,
        outcome=completion.status.value,
        detail={"error": completion.error} if completion.error else {},
        request=request,
    )
    return task


# --------------------------------------------------------------------------- #
# Operator plane - fleet
# --------------------------------------------------------------------------- #
@app.get("/api/v1/agents", response_model=list[AgentView], tags=["fleet"])
async def list_agents(
    agent_status: AgentStatus | None = Query(default=None, alias="status"),
    framework: str | None = None,
    platform: AgentPlatform | None = None,
    principal: Principal = Depends(require_operator),
) -> list[AgentView]:
    return registry.list_agents(status=agent_status, framework=framework, platform=platform)


@app.get("/api/v1/agents/{agent_id}", response_model=AgentView, tags=["fleet"])
async def get_agent(agent_id: str, principal: Principal = Depends(require_operator)) -> AgentView:
    agent = registry.get(agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return agent


@app.post("/api/v1/agents/{agent_id}/drain", response_model=AgentView, tags=["fleet"])
async def drain_agent(
    agent_id: str, request: Request, principal: Principal = Depends(require_operator)
) -> AgentView:
    if not registry.set_status(agent_id, AgentStatus.DRAINING):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    audit.record(principal.identifier, "agent.drain", "agent", agent_id, request=request)
    agent = registry.get(agent_id)
    assert agent is not None
    return agent


@app.delete("/api/v1/agents/{agent_id}", tags=["fleet"])
async def deregister_agent(
    agent_id: str, request: Request, principal: Principal = Depends(require_operator)
) -> dict[str, str]:
    if not registry.deregister(agent_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    audit.record(principal.identifier, "agent.deregister", "agent", agent_id, request=request)
    return {"status": "deregistered", "agent_id": agent_id}


# --------------------------------------------------------------------------- #
# Operator plane - tasks
# --------------------------------------------------------------------------- #
@app.post(
    "/api/v1/tasks",
    response_model=TaskView,
    status_code=status.HTTP_201_CREATED,
    tags=["tasks"],
)
async def submit_task(
    submission: TaskSubmission,
    request: Request,
    principal: Principal = Depends(require_operator),
) -> TaskView:
    task = tasks.create(submission, principal.identifier)
    audit.record(
        principal.identifier,
        "task.submit",
        "task",
        task.task_id,
        detail={"action": task.action, "capabilities": task.required_capabilities},
        request=request,
    )
    return task


@app.post("/api/v1/tasks/preview-routing", response_model=RoutingPreview, tags=["tasks"])
async def preview_routing(
    submission: TaskSubmission, principal: Principal = Depends(require_operator)
) -> RoutingPreview:
    """Dry-run the routing engine without queueing anything."""
    probe = TaskView(
        task_id="preview",
        title=submission.title,
        action=submission.action,
        payload=submission.payload,
        required_capabilities=submission.required_capabilities,
        preferred_framework=submission.preferred_framework,
        target_agent_id=submission.target_agent_id,
        label_selector=submission.label_selector,
        priority=submission.priority,
        status=TaskStatus.PENDING,
        attempts=0,
        max_attempts=submission.max_attempts,
        timeout_seconds=submission.timeout_seconds or get_settings().default_task_timeout,
        submitted_by=principal.identifier,
        created_at="",
        updated_at="",
        requested_model=submission.requested_model,
        required_model_capabilities=submission.required_model_capabilities,
    )
    decision = select_agent(probe, registry.list_agents())
    model_decision = select_model(probe, foundry.list_deployments(status=ModelStatus.AVAILABLE))
    return RoutingPreview(
        agent_id=decision.agent.agent_id if decision.agent else None,
        agent_name=decision.agent.name if decision.agent else None,
        score=decision.score,
        reason=decision.reason,
        model_deployment=model_decision.deployment.name if model_decision.deployment else None,
        model_name=model_decision.deployment.model_name if model_decision.deployment else None,
        model_reason=model_decision.reason,
    )


@app.get("/api/v1/tasks", response_model=list[TaskView], tags=["tasks"])
async def list_tasks(
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    agent_id: str | None = None,
    workflow_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_operator),
) -> list[TaskView]:
    return tasks.list_tasks(
        status=task_status, agent_id=agent_id, workflow_id=workflow_id, limit=limit
    )


@app.get("/api/v1/tasks/{task_id}", response_model=TaskView, tags=["tasks"])
async def get_task(task_id: str, principal: Principal = Depends(require_operator)) -> TaskView:
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return task


@app.get("/api/v1/tasks/{task_id}/events", response_model=list[TaskEvent], tags=["tasks"])
async def get_task_events(
    task_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    principal: Principal = Depends(require_operator),
) -> list[TaskEvent]:
    if tasks.get(task_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return tasks.events(task_id, limit)


@app.post("/api/v1/tasks/{task_id}/cancel", response_model=TaskView, tags=["tasks"])
async def cancel_task(
    task_id: str, request: Request, principal: Principal = Depends(require_operator)
) -> TaskView:
    task = tasks.cancel(task_id, principal.identifier)
    if task is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "task not found or already finished")
    audit.record(principal.identifier, "task.cancel", "task", task_id, request=request)
    if task.workflow_id:
        workflows.advance(task.workflow_id)
    return task


# --------------------------------------------------------------------------- #
# Operator plane - workflows
# --------------------------------------------------------------------------- #
@app.post(
    "/api/v1/workflows",
    response_model=WorkflowView,
    status_code=status.HTTP_201_CREATED,
    tags=["workflows"],
)
async def submit_workflow(
    submission: WorkflowSubmission,
    request: Request,
    principal: Principal = Depends(require_operator),
) -> WorkflowView:
    workflow = workflows.create(submission, principal.identifier)
    audit.record(
        principal.identifier,
        "workflow.submit",
        "workflow",
        workflow.workflow_id,
        detail={"name": workflow.name, "steps": len(submission.steps)},
        request=request,
    )
    return workflow


@app.get("/api/v1/workflows", response_model=list[WorkflowView], tags=["workflows"])
async def list_workflows(
    workflow_status: WorkflowStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    principal: Principal = Depends(require_operator),
) -> list[WorkflowView]:
    return workflows.list_workflows(status=workflow_status, limit=limit)


@app.get("/api/v1/workflows/{workflow_id}", response_model=WorkflowView, tags=["workflows"])
async def get_workflow(
    workflow_id: str, principal: Principal = Depends(require_operator)
) -> WorkflowView:
    workflow = workflows.get(workflow_id)
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workflow not found")
    return workflow


@app.post("/api/v1/workflows/{workflow_id}/cancel", response_model=WorkflowView, tags=["workflows"])
async def cancel_workflow(
    workflow_id: str, request: Request, principal: Principal = Depends(require_operator)
) -> WorkflowView:
    workflow = workflows.cancel(workflow_id, principal.identifier)
    if workflow is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "workflow not found or already finished")
    audit.record(principal.identifier, "workflow.cancel", "workflow", workflow_id, request=request)
    return workflow


# --------------------------------------------------------------------------- #
# Operator plane - Microsoft Foundry model catalog
# --------------------------------------------------------------------------- #
@app.get("/api/v1/models", response_model=list[ModelDeploymentView], tags=["models"])
async def list_models(
    model_status: ModelStatus | None = Query(default=None, alias="status"),
    principal: Principal = Depends(require_operator),
) -> list[ModelDeploymentView]:
    """Deployments mirrored from the configured Microsoft Foundry project."""
    return foundry.list_deployments(status=model_status)


@app.get("/api/v1/models/{name}", response_model=ModelDeploymentView, tags=["models"])
async def get_model(name: str, principal: Principal = Depends(require_operator)) -> ModelDeploymentView:
    deployment = foundry.get(name)
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deployment not in the catalog")
    return deployment


@app.post("/api/v1/models/sync", response_model=ModelSyncResult, tags=["models"])
async def sync_models(
    request: Request, principal: Principal = Depends(require_operator)
) -> ModelSyncResult:
    """Force an immediate re-sync against Foundry."""
    result = await foundry.sync_async()
    audit.record(
        principal.identifier,
        "models.sync",
        "model_catalog",
        result.source,
        outcome=result.status,
        detail=result.model_dump(mode="json"),
        request=request,
    )
    return result


# --------------------------------------------------------------------------- #
# Operator plane - governance & telemetry
# --------------------------------------------------------------------------- #
@app.get("/api/v1/audit", response_model=list[AuditEntry], tags=["governance"])
async def get_audit_log(
    entity_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    principal: Principal = Depends(require_operator),
) -> list[AuditEntry]:
    return audit.recent(limit=limit, entity_id=entity_id)


@app.get("/api/v1/metrics/summary", response_model=FleetSummary, tags=["governance"])
async def get_summary(principal: Principal = Depends(require_operator)) -> FleetSummary:
    return metrics.fleet_summary()


if os.path.isdir(STATIC_DIR):
    app.mount("/dashboard", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")
