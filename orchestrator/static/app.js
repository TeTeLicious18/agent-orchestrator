/* Agent Orchestrator dashboard.
   All rendering goes through textContent / DOM APIs - never innerHTML - so agent
   supplied strings can never execute in the operator's browser. */
(() => {
    "use strict";

    const API = "/api/v1";
    const REFRESH_MS = 3000;

    let apiKey = localStorage.getItem("orch.apiKey") || "";
    let selectedTaskId = null;
    let timer = null;

    const $ = (id) => document.getElementById(id);

    // ----------------------------------------------------------------- utils
    function el(tag, opts = {}, children = []) {
        const node = document.createElement(tag);
        if (opts.class) node.className = opts.class;
        if (opts.text !== undefined) node.textContent = opts.text;
        if (opts.title) node.title = opts.title;
        if (opts.dataset) Object.assign(node.dataset, opts.dataset);
        children.forEach((child) => node.appendChild(child));
        return node;
    }

    const pill = (value) => el("span", { class: `pill pill-${String(value)}`, text: String(value) });

    function relative(iso) {
        if (!iso) return "-";
        const then = new Date(iso).getTime();
        if (Number.isNaN(then)) return "-";
        const secs = Math.round((Date.now() - then) / 1000);
        if (secs < 5) return "just now";
        if (secs < 60) return `${secs}s ago`;
        if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
        if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
        return new Date(iso).toLocaleString();
    }

    function progressCell(percent) {
        const fill = el("span");
        fill.style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
        return el("div", { class: "bar", title: `${percent}%` }, [fill]);
    }

    function setConnection(state, label) {
        const node = $("connState");
        node.className = `pill pill-${state}`;
        node.textContent = label;
    }

    async function api(path, options = {}) {
        const response = await fetch(`${API}${path}`, {
            ...options,
            headers: {
                "Content-Type": "application/json",
                "X-API-Key": apiKey,
                ...(options.headers || {}),
            },
        });
        if (!response.ok) {
            let detail = `${response.status} ${response.statusText}`;
            try {
                const body = await response.json();
                if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
            } catch (_) { /* keep status text */ }
            const error = new Error(detail);
            error.status = response.status;
            throw error;
        }
        return response.status === 204 ? null : response.json();
    }

    function replaceRows(tableId, rows) {
        const body = $(tableId).tBodies[0];
        body.replaceChildren(...rows);
    }

    function emptyRow(colspan, message) {
        const cell = el("td", { text: message, class: "hint" });
        cell.colSpan = colspan;
        return el("tr", {}, [cell]);
    }

    // --------------------------------------------------------------- renders
    function renderKpis(summary) {
        const running = (summary.tasks_by_status.running || 0) + (summary.tasks_by_status.assigned || 0);
        const queued = (summary.tasks_by_status.pending || 0) + (summary.tasks_by_status.blocked || 0);
        const cards = [
            ["Agents online", `${summary.agents_online}/${summary.agents_total}`],
            ["Foundry models", `${summary.models_available}/${summary.models_total}`],
            ["Tasks running", String(running)],
            ["Queued", String(queued)],
            ["Succeeded", String(summary.tasks_by_status.succeeded || 0)],
            ["Failed", String((summary.tasks_by_status.failed || 0) + (summary.tasks_by_status.timed_out || 0))],
            ["Tokens used", summary.tokens_total.toLocaleString()],
            ["Avg duration", summary.avg_duration_seconds === null ? "-" : `${summary.avg_duration_seconds}s`],
            ["Success rate", summary.success_rate === null ? "-" : `${Math.round(summary.success_rate * 100)}%`],
        ].map(([label, value]) =>
            el("div", { class: "kpi" }, [
                el("div", { class: "value", text: value }),
                el("div", { class: "label", text: label }),
            ])
        );
        $("kpis").replaceChildren(...cards);
    }

    function renderModels(models, summary) {
        const select = $("model");
        const chosen = select.value;
        const options = [
            el("option", { text: "Auto-select from Foundry" }),
            el("option", { text: "No model needed" }),
        ];
        options[0].value = "";
        options[1].value = "__none__";
        models
            .filter((model) => model.status === "available")
            .forEach((model) => {
                const option = el("option", { text: `${model.name} (${model.model_name})` });
                option.value = model.name;
                options.push(option);
            });
        select.replaceChildren(...options);
        select.value = chosen;
        if (!select.value) select.value = "";

        const sources = new Set(models.map((model) => model.source));
        $("modelSource").textContent = models.length
            ? `${models.length} deployment(s) from ${[...sources].join(", ")}`
            : "catalog empty - set FOUNDRY_PROJECT_ENDPOINT to sync";

        if (!models.length) {
            replaceRows("modelsTable", [
                emptyRow(8, "No Foundry deployments synced yet. Configure the project endpoint, then Sync now."),
            ]);
            return;
        }
        const tokens = summary.tokens_by_model || {};
        replaceRows(
            "modelsTable",
            models.map((model) =>
                el("tr", {}, [
                    el("td", { class: "mono", text: model.name }),
                    el("td", { text: model.model_name }),
                    el("td", { text: model.publisher || "-" }),
                    el("td", { text: model.model_version || "-" }),
                    el("td", { class: "hint", text: model.capabilities.join(", ") }),
                    el("td", {}, [pill(model.status)]),
                    el("td", { text: (tokens[model.name] || 0).toLocaleString() }),
                    el("td", { text: relative(model.synced_at), title: model.synced_at }),
                ])
            )
        );
    }

    let onlineAgentIds = [];

    function renderAgentPicker(agents) {
        const select = $("targetAgent");
        const chosen = select.value;
        onlineAgentIds = agents.filter((agent) => agent.status === "online").map((agent) => agent.agent_id);

        const auto = el("option", { text: "Auto - best match" });
        auto.value = "";
        const all = el("option", { text: `Every online agent (${onlineAgentIds.length}) - one task each` });
        all.value = "__all__";
        all.disabled = onlineAgentIds.length === 0;

        const options = [auto, all];
        agents.forEach((agent) => {
            const option = el("option", {
                text: `${agent.agent_id} - ${agent.framework} / ${agent.platform}${agent.status === "online" ? "" : " (offline)"}`,
            });
            option.value = agent.agent_id;
            option.disabled = agent.status !== "online";
            options.push(option);
        });

        select.replaceChildren(...options);
        // Keep the operator's choice across the polling refresh.
        select.value = Array.from(select.options).some((option) => option.value === chosen) ? chosen : "";
    }

    function renderAgents(agents) {
        $("agentCount").textContent = `${agents.length} registered`;
        renderAgentPicker(agents);
        if (!agents.length) {
            replaceRows("agentsTable", [emptyRow(8, "No agents registered yet. Start an agent node to join the fleet.")]);
            return;
        }
        replaceRows(
            "agentsTable",
            agents.map((agent) =>
                el("tr", {}, [
                    el("td", {}, [
                        el("div", { text: agent.name }),
                        el("div", { class: "hint mono", text: agent.agent_id }),
                    ]),
                    el("td", { text: agent.framework }),
                    el("td", { text: agent.platform }),
                    el("td", { text: agent.host }),
                    el("td", { class: "hint", text: agent.capabilities.join(", ") || "-" }),
                    el("td", { text: `${agent.active_tasks}/${agent.max_concurrency}` }),
                    el("td", {}, [pill(agent.status)]),
                    el("td", { text: relative(agent.last_heartbeat), title: agent.last_heartbeat || "" }),
                ])
            )
        );
    }

    function renderTasks(tasks) {
        if (!tasks.length) {
            replaceRows("tasksTable", [emptyRow(7, "No tasks submitted yet.")]);
            return;
        }
        replaceRows(
            "tasksTable",
            tasks.map((task) => {
                const row = el("tr", { dataset: { taskId: task.task_id } }, [
                    el("td", {}, [
                        el("div", { text: task.title }),
                        el("div", { class: "hint mono", text: task.task_id }),
                    ]),
                    el("td", { class: "mono", text: task.action }),
                    el("td", { class: "mono", text: task.agent_id || "-" }),
                    el("td", { class: "mono", text: task.model_deployment || "-" }),
                    el("td", {}, [pill(task.status)]),
                    el("td", {}, [progressCell(task.progress)]),
                    el("td", { text: relative(task.updated_at), title: task.updated_at }),
                ]);
                if (task.task_id === selectedTaskId) row.classList.add("selected");
                row.addEventListener("click", () => {
                    selectedTaskId = task.task_id;
                    refresh();
                });
                return row;
            })
        );
    }

    function renderWorkflows(workflows) {
        if (!workflows.length) {
            replaceRows("workflowsTable", [emptyRow(5, "No workflows yet.")]);
            return;
        }
        replaceRows(
            "workflowsTable",
            workflows.map((wf) => {
                const done = wf.tasks.filter((t) => t.status === "succeeded").length;
                const percent = wf.tasks.length ? Math.round((done / wf.tasks.length) * 100) : 0;
                return el("tr", {}, [
                    el("td", {}, [
                        el("div", { text: wf.name }),
                        el("div", { class: "hint mono", text: wf.workflow_id }),
                    ]),
                    el("td", {}, [pill(wf.status)]),
                    el("td", { text: `${done}/${wf.tasks.length}` }),
                    el("td", {}, [progressCell(percent)]),
                    el("td", { text: relative(wf.updated_at), title: wf.updated_at }),
                ]);
            })
        );
    }

    function renderAudit(entries) {
        if (!entries.length) {
            replaceRows("auditTable", [emptyRow(6, "No audit entries yet.")]);
            return;
        }
        replaceRows(
            "auditTable",
            entries.map((entry) =>
                el("tr", {}, [
                    el("td", { text: relative(entry.ts), title: entry.ts }),
                    el("td", { class: "mono", text: entry.actor }),
                    el("td", { text: entry.action }),
                    el("td", { class: "mono", text: `${entry.entity_type}:${entry.entity_id || "-"}` }),
                    el("td", {}, [pill(entry.outcome)]),
                    el("td", { class: "mono", text: entry.source_ip || "-" }),
                ])
            )
        );
    }

    async function renderDetail() {
        const panel = $("detail");
        const head = el("div", { class: "panel-head" }, [el("h2", { text: "Execution detail" })]);
        if (!selectedTaskId) {
            panel.replaceChildren(head, el("p", { class: "hint", text: "No task selected." }));
            return;
        }
        const [task, events] = await Promise.all([
            api(`/tasks/${encodeURIComponent(selectedTaskId)}`),
            api(`/tasks/${encodeURIComponent(selectedTaskId)}/events`),
        ]);

        const definitions = el("dl");
        const add = (term, value) => {
            definitions.appendChild(el("dt", { text: term }));
            definitions.appendChild(el("dd", { text: value }));
        };
        add("Task", task.title);
        add("Action", task.action);
        add("Status", task.status);
        add("Agent", task.agent_id || "unassigned");
        add("Attempts", `${task.attempts}/${task.max_attempts}`);
        add("Priority", String(task.priority));
        add("Capabilities", task.required_capabilities.join(", ") || "inferred");
        if (task.model_deployment) {
            add("Foundry model", task.model_deployment);
            add("Tokens", `${task.prompt_tokens} prompt / ${task.completion_tokens} completion`);
        } else if (task.required_model_capabilities.length) {
            add("Model needs", task.required_model_capabilities.join(", "));
        }
        if (task.workflow_id) add("Workflow", `${task.workflow_id} (${task.step_id})`);
        if (task.error) add("Error", task.error);

        const children = [head, definitions];
        if (task.result) {
            children.push(el("h2", { text: "Result" }));
            children.push(el("pre", { text: JSON.stringify(task.result, null, 2) }));
        }
        children.push(el("h2", { text: "Telemetry" }));
        children.push(
            el(
                "ul",
                { class: "events" },
                events.length
                    ? events
                          .slice()
                          .reverse()
                          .map((event) =>
                              el("li", {}, [
                                  el("span", { class: "kind", text: event.kind }),
                                  el("span", { class: "ts", text: relative(event.ts) }),
                                  el("span", { class: "msg", text: event.message }),
                              ])
                          )
                    : [el("li", { class: "hint", text: "No events recorded." })]
            )
        );

        const cancel = el("button", { class: "secondary", text: "Cancel task" });
        cancel.addEventListener("click", async () => {
            try {
                await api(`/tasks/${encodeURIComponent(task.task_id)}/cancel`, { method: "POST" });
                refresh();
            } catch (error) {
                setMessage(error.message, "error");
            }
        });
        if (!["succeeded", "failed", "cancelled", "timed_out"].includes(task.status)) {
            children.push(cancel);
        }
        panel.replaceChildren(...children);
    }

    // ----------------------------------------------------------------- form
    function setMessage(text, kind = "") {
        const node = $("formMessage");
        node.textContent = text;
        node.className = `form-message ${kind}`;
    }

    // The plain-language box is the primary input; everything under "Advanced" only
    // overrides what would otherwise be derived from it.
    const DEFAULT_ACTION = "agent.do";

    function titleFrom(instruction) {
        const firstLine = instruction.split("\n")[0].trim();
        return firstLine.length > 80 ? `${firstLine.slice(0, 77)}...` : firstLine;
    }

    function readSubmission() {
        const instruction = $("instruction").value.trim();
        const action = $("action").value.trim() || (instruction ? DEFAULT_ACTION : "");
        if (!action) {
            throw new Error("Describe what the fleet should do, or set an action under Advanced.");
        }

        let payload = {};
        const raw = $("payload").value.trim();
        if (raw) {
            payload = JSON.parse(raw); // surfaced to the user as a validation error
            if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
                throw new Error("Payload must be a JSON object");
            }
        } else if (instruction) {
            // 'goal' drives the autonomous adapter; 'prompt' covers the model-backed ones.
            payload = action.startsWith("agent.") ? { goal: instruction } : { prompt: instruction };
        }

        const capabilities = $("capabilities").value
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean);

        const title = $("title").value.trim() || titleFrom(instruction) || action;

        const submission = {
            title,
            action,
            payload,
            required_capabilities: capabilities,
            priority: Number($("priority").value) || 5,
        };
        const framework = $("framework").value;
        if (framework) submission.preferred_framework = framework;

        const target = $("targetAgent").value;
        if (target && target !== "__all__") submission.target_agent_id = target;

        const model = $("model").value;
        const modelCapabilities = $("modelCapabilities").value
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean);
        if (model === "__none__") {
            // Explicit opt-out: the task runs without a Foundry binding.
        } else if (model) {
            submission.requested_model = model;
            if (modelCapabilities.length) submission.required_model_capabilities = modelCapabilities;
        } else {
            submission.required_model_capabilities = modelCapabilities.length ? modelCapabilities : ["chat"];
        }
        return submission;
    }

    $("taskForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        try {
            const submission = readSubmission();

            if ($("targetAgent").value === "__all__") {
                if (!onlineAgentIds.length) throw new Error("No online agent to dispatch to.");
                const ids = [];
                for (const agentId of onlineAgentIds) {
                    const task = await api("/tasks", {
                        method: "POST",
                        body: JSON.stringify({ ...submission, target_agent_id: agentId }),
                    });
                    ids.push(`${agentId}:${task.task_id}`);
                }
                selectedTaskId = null;
                setMessage(`Dispatched to ${ids.length} agent(s) - ${ids.join(", ")}`, "ok");
            } else {
                const task = await api("/tasks", { method: "POST", body: JSON.stringify(submission) });
                selectedTaskId = task.task_id;
                setMessage(
                    `Task ${task.task_id} queued` +
                        (submission.target_agent_id ? ` for ${submission.target_agent_id}.` : " for routing."),
                    "ok"
                );
            }

            $("instruction").value = "";
            refresh();
        } catch (error) {
            setMessage(error.message, "error");
        }
    });

    $("previewBtn").addEventListener("click", async () => {
        try {
            const preview = await api("/tasks/preview-routing", {
                method: "POST",
                body: JSON.stringify(readSubmission()),
            });
            setMessage(
                preview.agent_id
                    ? `Would route to ${preview.agent_name} (${preview.agent_id}), score ${preview.score}` +
                      (preview.model_deployment
                          ? ` · model ${preview.model_deployment} (${preview.model_reason})`
                          : "") +
                      ` - ${preview.reason}`
                    : `No eligible agent: ${preview.reason}`,
                preview.agent_id ? "ok" : "error"
            );
        } catch (error) {
            setMessage(error.message, "error");
        }
    });

    $("syncModels").addEventListener("click", async () => {
        try {
            const result = await api("/models/sync", { method: "POST" });
            setMessage(
                result.status === "synced"
                    ? `Foundry sync: ${result.discovered} deployment(s), +${result.added} new, ${result.retired} retired.`
                    : `Foundry sync ${result.status}: ${result.detail || ""}`,
                result.status === "synced" ? "ok" : "error"
            );
            refresh();
        } catch (error) {
            setMessage(error.message, "error");
        }
    });

    $("saveKey").addEventListener("click", () => {
        apiKey = $("apiKey").value.trim();
        localStorage.setItem("orch.apiKey", apiKey);
        refresh();
    });

    // The key field sits outside the task form, so Enter would otherwise do nothing.
    $("apiKey").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            $("saveKey").click();
        }
    });

    // --------------------------------------------------------------- polling
    async function refresh() {
        if (!apiKey) {
            setConnection("idle", "enter API key");
            return;
        }
        try {
            const [summary, agents, tasks, workflows, auditEntries, models] = await Promise.all([
                api("/metrics/summary"),
                api("/agents"),
                api("/tasks?limit=60"),
                api("/workflows?limit=20"),
                api("/audit?limit=40"),
                api("/models"),
            ]);
            renderKpis(summary);
            renderModels(models, summary);
            renderAgents(agents);
            renderTasks(tasks);
            renderWorkflows(workflows);
            renderAudit(auditEntries);
            await renderDetail();
            setConnection("online", "connected");
        } catch (error) {
            setConnection("offline", error.status === 401 ? "unauthorized" : "error");
        }
    }

    function start() {
        $("apiKey").value = apiKey;
        refresh();
        if (timer) clearInterval(timer);
        timer = setInterval(refresh, REFRESH_MS);
    }

    document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
            clearInterval(timer);
            timer = null;
        } else if (!timer) {
            start();
        }
    });

    start();
})();
