"""Capability-aware routing engine.

The orchestrator decides *which* agent should run *which* task. Matching is a
two phase process:

1. **Eligibility** - hard constraints (explicit target, capabilities, label
   selector, liveness, concurrency headroom) filter the fleet.
2. **Scoring** - remaining candidates are ranked on framework affinity, spare
   capacity, label specificity and heartbeat freshness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .config import get_settings
from .models import AgentStatus, AgentView, ModelDeploymentView, ModelStatus, TaskView

# An agent must advertise this capability before the orchestrator will hand it a
# task bound to a Foundry model deployment.
MODEL_AGENT_CAPABILITY = "foundry.inference"

# Actions are namespaced (``browser.navigate``, ``fs.write`` ...). When a caller
# does not declare capabilities explicitly we infer them from the namespace so
# that simple submissions still route correctly.
CAPABILITY_HINTS: dict[str, tuple[str, ...]] = {
    "agent": ("autonomy",),
    "browser": ("browser.automation",),
    "web": ("browser.automation",),
    "research": ("research", "browser.automation"),
    "fs": ("filesystem",),
    "file": ("filesystem",),
    "shell": ("shell.exec",),
    "script": ("shell.exec",),
    "powershell": ("shell.exec", "windows"),
    "sysadmin": ("system.admin",),
    "admin": ("system.admin",),
    "http": ("http",),
    "api": ("http",),
    "data": ("data.analysis",),
    "report": ("reporting",),
    "summarize": ("llm.reasoning",),
    "plan": ("llm.reasoning",),
}


@dataclass(frozen=True)
class RoutingDecision:
    agent: AgentView | None
    score: float
    reason: str


@dataclass(frozen=True)
class ModelDecision:
    deployment: ModelDeploymentView | None
    score: float
    reason: str


def requires_model(task: TaskView) -> bool:
    return bool(task.requested_model or task.required_model_capabilities)


def infer_capabilities(action: str, declared: list[str]) -> list[str]:
    if declared:
        return declared
    namespace = action.split(".", 1)[0].strip().lower()
    return list(CAPABILITY_HINTS.get(namespace, ()))


def _labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in selector.items())


def is_eligible(agent: AgentView, task: TaskView) -> tuple[bool, str]:
    """Hard constraint check for a single (agent, task) pair."""
    if task.target_agent_id and task.target_agent_id != agent.agent_id:
        return False, "pinned to a different agent"
    if agent.status is not AgentStatus.ONLINE:
        return False, f"agent is {agent.status.value}"
    if agent.active_tasks >= agent.max_concurrency:
        return False, "no concurrency headroom"
    if not _labels_match(task.label_selector, agent.labels):
        return False, "label selector not satisfied"

    required = set(infer_capabilities(task.action, task.required_capabilities))
    if requires_model(task):
        required.add(MODEL_AGENT_CAPABILITY)
    missing = required - set(agent.capabilities)
    if missing:
        return False, f"missing capabilities: {', '.join(sorted(missing))}"
    return True, "eligible"


def score(agent: AgentView, task: TaskView) -> float:
    value = 100.0

    if task.preferred_framework and agent.framework == task.preferred_framework:
        value += 40.0

    # Prefer the least loaded node so work spreads across the fleet.
    headroom = agent.max_concurrency - agent.active_tasks
    value += 12.0 * headroom / max(agent.max_concurrency, 1)

    # Reward specialists: an agent advertising exactly what is needed beats a
    # generalist that happens to also match.
    required = set(infer_capabilities(task.action, task.required_capabilities))
    if required:
        specificity = len(required) / max(len(agent.capabilities), 1)
        value += 15.0 * min(specificity, 1.0)

    value += 5.0 * len(set(task.label_selector.items()) & set(agent.labels.items()))

    if agent.last_heartbeat:
        try:
            beat = datetime.fromisoformat(agent.last_heartbeat)
            if beat.tzinfo is None:
                beat = beat.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - beat).total_seconds()
            value += max(0.0, 10.0 - age)
        except ValueError:
            pass

    return round(value, 3)


def select_agent(task: TaskView, agents: list[AgentView]) -> RoutingDecision:
    """Pick the best agent for a task, or explain why none fits."""
    if not agents:
        return RoutingDecision(None, 0.0, "no agents registered")

    candidates: list[tuple[float, AgentView]] = []
    rejections: list[str] = []
    for agent in agents:
        ok, reason = is_eligible(agent, task)
        if ok:
            candidates.append((score(agent, task), agent))
        elif reason != "pinned to a different agent":
            rejections.append(f"{agent.agent_id}: {reason}")

    if not candidates:
        detail = "; ".join(rejections[:5]) or "no matching agent"
        return RoutingDecision(None, 0.0, detail)

    candidates.sort(key=lambda item: (item[0], item[1].agent_id), reverse=True)
    best_score, best_agent = candidates[0]
    return RoutingDecision(best_agent, best_score, "best capability and capacity match")


def rank_tasks_for_agent(agent: AgentView, tasks: list[TaskView]) -> list[TaskView]:
    """Order the pending queue by suitability for a specific agent."""
    scored: list[tuple[int, float, str, TaskView]] = []
    for task in tasks:
        ok, _ = is_eligible(agent, task)
        if not ok:
            continue
        scored.append((task.priority, score(agent, task), task.created_at, task))
    # Lowest priority number first (1 = most urgent), then best score, then FIFO.
    scored.sort(key=lambda item: (item[0], -item[1], item[2]))
    return [item[3] for item in scored]


# --------------------------------------------------------------------------- #
# Foundry model selection
# --------------------------------------------------------------------------- #
def select_model(task: TaskView, deployments: list[ModelDeploymentView]) -> ModelDecision:
    """Choose the Foundry deployment that should back this task."""
    if not requires_model(task):
        return ModelDecision(None, 0.0, "task does not request a model")
    if not deployments:
        return ModelDecision(None, 0.0, "no Foundry deployments in the catalog")

    if task.requested_model:
        for deployment in deployments:
            if deployment.name != task.requested_model:
                continue
            if deployment.status is not ModelStatus.AVAILABLE:
                return ModelDecision(None, 0.0, f"deployment '{deployment.name}' is retired in Foundry")
            missing = set(task.required_model_capabilities) - set(deployment.capabilities)
            if missing:
                return ModelDecision(
                    None, 0.0, f"'{deployment.name}' lacks: {', '.join(sorted(missing))}"
                )
            return ModelDecision(deployment, 1000.0, "pinned by the submitter")
        return ModelDecision(None, 0.0, f"deployment '{task.requested_model}' is not in the catalog")

    preference = get_settings().model_preference
    required = set(task.required_model_capabilities)
    candidates: list[tuple[float, ModelDeploymentView]] = []
    gaps: list[str] = []

    for deployment in deployments:
        if deployment.status is not ModelStatus.AVAILABLE:
            continue
        missing = required - set(deployment.capabilities)
        if missing:
            gaps.append(f"{deployment.name}: missing {', '.join(sorted(missing))}")
            continue

        score = 100.0
        for index, preferred in enumerate(preference):
            if preferred in (deployment.name, deployment.model_name):
                score += 100.0 - index
                break
        # Prefer the least over-provisioned model that still satisfies the need.
        score -= 2.0 * max(0, len(deployment.capabilities) - len(required))
        candidates.append((score, deployment))

    if not candidates:
        detail = "; ".join(gaps[:5]) or "no available deployment satisfies the requested capabilities"
        return ModelDecision(None, 0.0, detail)

    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    best_score, best = candidates[0]
    return ModelDecision(best, round(best_score, 3), "best capability match in the Foundry catalog")
