"""Pydantic schemas shared by the control plane API and the agent runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat()


class AgentFramework(str, Enum):
    SCOUT = "scout"
    CLAWDBOT = "clawdbot"
    BUILTIN = "builtin"
    CUSTOM = "custom"


class AgentPlatform(str, Enum):
    AZURE_VM = "azure_vm"
    NETWORK_PC = "network_pc"
    CONTAINER = "container"
    OTHER = "other"


class AgentStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DRAINING = "draining"


class TaskStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    ASSIGNED = "assigned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.TIMED_OUT,
}


class WorkflowStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


# Normalised capability vocabulary the router matches on. Foundry reports
# per-deployment capability flags which are mapped onto these names.
MODEL_CAPABILITIES = (
    "chat",
    "reasoning",
    "tool-calling",
    "json-mode",
    "vision",
    "audio",
    "embeddings",
    "image-generation",
    "fine-tuning",
)


# --------------------------------------------------------------------------- #
# Agent registration & telemetry
# --------------------------------------------------------------------------- #
class AgentRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)
    framework: AgentFramework
    platform: AgentPlatform = AgentPlatform.OTHER
    host: str = Field(default="unknown", max_length=255)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    labels: dict[str, str] = Field(default_factory=dict)
    max_concurrency: int = Field(default=1, ge=1, le=64)
    version: str | None = Field(default=None, max_length=40)

    @field_validator("capabilities")
    @classmethod
    def _normalise_caps(cls, value: list[str]) -> list[str]:
        return sorted({cap.strip().lower() for cap in value if cap.strip()})


class AgentView(BaseModel):
    agent_id: str
    name: str
    framework: AgentFramework
    platform: AgentPlatform
    host: str
    capabilities: list[str]
    labels: dict[str, str]
    max_concurrency: int
    status: AgentStatus
    version: str | None = None
    registered_at: str
    last_heartbeat: str | None = None
    active_tasks: int = 0


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AgentStatus = AgentStatus.ONLINE
    active_tasks: int = Field(default=0, ge=0)
    metrics: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
class TaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    preferred_framework: AgentFramework | None = None
    target_agent_id: str | None = Field(default=None, max_length=64)
    label_selector: dict[str, str] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=1, le=9)
    max_attempts: int = Field(default=2, ge=1, le=10)
    timeout_seconds: int | None = Field(default=None, ge=5, le=86_400)
    # Foundry model selection: either pin a deployment by name, or state the
    # model capabilities the work needs and let the orchestrator choose.
    requested_model: str | None = Field(default=None, max_length=120)
    required_model_capabilities: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("required_capabilities", "required_model_capabilities")
    @classmethod
    def _normalise_caps(cls, value: list[str]) -> list[str]:
        return sorted({cap.strip().lower() for cap in value if cap.strip()})


class TaskView(BaseModel):
    task_id: str
    workflow_id: str | None = None
    step_id: str | None = None
    title: str
    action: str
    payload: dict[str, Any]
    required_capabilities: list[str]
    preferred_framework: AgentFramework | None = None
    target_agent_id: str | None = None
    label_selector: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    priority: int
    status: TaskStatus
    agent_id: str | None = None
    attempts: int
    max_attempts: int
    timeout_seconds: int
    progress: int = 0
    submitted_by: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    requested_model: str | None = None
    required_model_capabilities: list[str] = Field(default_factory=list)
    model_deployment: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelBinding(BaseModel):
    """Everything an agent needs to call a Foundry model - and nothing secret.

    Authentication happens on the node with its own managed identity, so no key
    or token is ever transmitted to, or stored on, an agent machine.
    """

    deployment_name: str
    model_name: str
    publisher: str | None = None
    model_version: str | None = None
    endpoint: str
    project: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    auth: str = "entra-id"


class TaskAssignment(BaseModel):
    """The slimmed down envelope handed to an agent when it leases work."""

    task_id: str
    workflow_id: str | None = None
    title: str
    action: str
    payload: dict[str, Any]
    timeout_seconds: int
    lease_expires_at: str
    attempt: int
    model: ModelBinding | None = None


class TaskProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    progress: int | None = Field(default=None, ge=0, le=100)
    message: str = Field(default="", max_length=2000)
    telemetry: dict[str, Any] = Field(default_factory=dict)


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)


class TaskCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TaskStatus
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=4000)
    usage: ModelUsage | None = None

    @field_validator("status")
    @classmethod
    def _terminal_only(cls, value: TaskStatus) -> TaskStatus:
        if value not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
            raise ValueError("agents may only report 'succeeded' or 'failed'")
        return value


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tasks: int = Field(default=1, ge=1, le=8)
    wait_seconds: int = Field(default=20, ge=0, le=60)


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #
class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    preferred_framework: AgentFramework | None = None
    target_agent_id: str | None = Field(default=None, max_length=64)
    label_selector: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    priority: int = Field(default=5, ge=1, le=9)
    max_attempts: int = Field(default=2, ge=1, le=10)
    timeout_seconds: int | None = Field(default=None, ge=5, le=86_400)
    requested_model: str | None = Field(default=None, max_length=120)
    required_model_capabilities: list[str] = Field(default_factory=list, max_length=16)


class WorkflowSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=100)

    @field_validator("steps")
    @classmethod
    def _validate_dag(cls, steps: list[WorkflowStep]) -> list[WorkflowStep]:
        ids = [step.step_id for step in steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step_id values must be unique within a workflow")
        known = set(ids)
        for step in steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(f"step '{step.step_id}' depends on unknown steps: {sorted(unknown)}")
            if step.step_id in step.depends_on:
                raise ValueError(f"step '{step.step_id}' cannot depend on itself")

        # Kahn's algorithm - reject cycles up front so the scheduler cannot stall.
        pending = {step.step_id: set(step.depends_on) for step in steps}
        resolved: set[str] = set()
        progressed = True
        while pending and progressed:
            progressed = False
            for step_id, deps in list(pending.items()):
                if deps <= resolved:
                    resolved.add(step_id)
                    del pending[step_id]
                    progressed = True
        if pending:
            raise ValueError(f"workflow contains a dependency cycle: {sorted(pending)}")
        return steps


class WorkflowView(BaseModel):
    workflow_id: str
    name: str
    status: WorkflowStatus
    submitted_by: str
    created_at: str
    updated_at: str
    tasks: list[TaskView] = Field(default_factory=list)
    result: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #
class TaskEvent(BaseModel):
    ts: str
    kind: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class AuditEntry(BaseModel):
    ts: str
    actor: str
    action: str
    entity_type: str
    entity_id: str | None = None
    outcome: str = "ok"
    detail: dict[str, Any] = Field(default_factory=dict)
    source_ip: str | None = None


class ModelDeploymentView(BaseModel):
    name: str
    model_name: str
    publisher: str | None = None
    model_version: str | None = None
    sku: str | None = None
    deployment_type: str | None = None
    endpoint: str
    project: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    status: ModelStatus = ModelStatus.AVAILABLE
    source: str = "foundry"
    first_seen_at: str
    synced_at: str


class ModelSyncResult(BaseModel):
    status: str  # synced | disabled | error
    source: str
    endpoint: str | None = None
    discovered: int = 0
    added: int = 0
    updated: int = 0
    retired: int = 0
    detail: str | None = None
    synced_at: str = Field(default_factory=utcnow_iso)


class FleetSummary(BaseModel):
    agents_total: int
    agents_online: int
    agents_by_framework: dict[str, int]
    agents_by_platform: dict[str, int]
    tasks_by_status: dict[str, int]
    workflows_by_status: dict[str, int]
    tasks_last_hour: int
    avg_duration_seconds: float | None = None
    success_rate: float | None = None
    models_total: int = 0
    models_available: int = 0
    tokens_by_model: dict[str, int] = Field(default_factory=dict)
    tokens_total: int = 0
    generated_at: str = Field(default_factory=utcnow_iso)
