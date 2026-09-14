"""Demo driver: submits a multi-agent workflow and streams the outcome.

Usage (with the orchestrator and both example agents running)::

    python -m tools.demo --api-key <key>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

WORKFLOW = {
    "name": "Quarterly infrastructure review",
    "steps": [
        {
            "step_id": "research",
            "title": "Research current Azure VM patch guidance",
            "action": "research.web",
            "payload": {"topic": "Azure VM patch compliance best practices"},
            "preferred_framework": "scout",
            "priority": 3,
        },
        {
            "step_id": "inventory",
            "title": "Collect host inventory from the on-prem PC",
            "action": "shell.exec",
            "payload": {"command": "host_info"},
            "required_capabilities": ["shell.exec"],
            "priority": 3,
        },
        {
            "step_id": "remediate",
            "title": "Draft the remediation runbook",
            "action": "automation.runbook",
            "payload": {"format": "markdown"},
            "preferred_framework": "clawdbot",
            "depends_on": ["research", "inventory"],
        },
        {
            "step_id": "report",
            "title": "Publish the consolidated report",
            "action": "report.publish",
            "payload": {"audience": "operations leadership"},
            "preferred_framework": "scout",
            "depends_on": ["remediate"],
        },
    ],
}

# Added only when the Foundry catalog has a chat-capable deployment, so the demo
# still completes when no Foundry project is configured.
MODEL_STEP = {
    "step_id": "synthesize",
    "title": "Summarise the review with a Foundry model",
    "action": "foundry.chat",
    "payload": {
        "system": "You are an infrastructure operations analyst.",
        "prompt": "Summarise the quarterly infrastructure review in five bullet points.",
    },
    "required_model_capabilities": ["chat"],
    "depends_on": ["report"],
}


async def run(base_url: str, api_key: str, timeout: float) -> int:
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0) as client:
        agents = (await client.get("/api/v1/agents", headers=headers)).json()
        if not agents:
            print("No agents are registered. Start at least one agent node first.")
            return 1
        print(f"Fleet: {len(agents)} agent(s)")
        for agent in agents:
            print(f"  - {agent['agent_id']:<18} {agent['framework']:<9} {agent['platform']:<12} {agent['status']}")

        models = (await client.get("/api/v1/models?status=available", headers=headers)).json()
        workflow = {**WORKFLOW, "steps": list(WORKFLOW["steps"])}
        if models:
            print(f"\nFoundry catalog: {len(models)} available deployment(s)")
            for model in models:
                print(f"  - {model['name']:<26} {model['model_name']:<24} {', '.join(model['capabilities'])}")
            if any("chat" in model["capabilities"] for model in models):
                workflow["steps"].append(MODEL_STEP)
        else:
            print("\nFoundry catalog empty - skipping the model-backed step.")

        response = await client.post("/api/v1/workflows", json=workflow, headers=headers)
        response.raise_for_status()
        workflow_id = response.json()["workflow_id"]
        print(f"\nSubmitted workflow {workflow_id}\n")

        seen: dict[str, str] = {}
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            workflow = (await client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)).json()
            for task in workflow["tasks"]:
                key = task["step_id"]
                state = f"{task['status']}@{task['agent_id']}@{task['model_deployment']}"
                if seen.get(key) != state:
                    seen[key] = state
                    model = f"  model={task['model_deployment']}" if task["model_deployment"] else ""
                    print(f"  {key:<12} {task['status']:<10} {task['agent_id'] or '-'}{model}")
            if workflow["status"] != "running":
                print(f"\nWorkflow {workflow['status']}")
                summary = (workflow.get("result") or {}).get("summary")
                if summary:
                    print(f"Step outcomes: {summary}")
                tokens = sum(t["prompt_tokens"] + t["completion_tokens"] for t in workflow["tasks"])
                if tokens:
                    print(f"Foundry tokens consumed: {tokens}")
                return 0 if workflow["status"] == "succeeded" else 1
            await asyncio.sleep(2)

        print("\nTimed out waiting for the workflow to finish.")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent orchestrator demo")
    parser.add_argument("--url", default=os.getenv("ORCH_URL", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.getenv("ORCH_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    api_key = args.api_key
    if not api_key and os.path.isfile(".dev-api-key"):
        api_key = open(".dev-api-key", encoding="utf-8").read().strip()
    if not api_key:
        raise SystemExit("Provide --api-key or set ORCH_API_KEY.")

    sys.exit(asyncio.run(run(args.url, api_key, args.timeout)))


if __name__ == "__main__":
    main()
