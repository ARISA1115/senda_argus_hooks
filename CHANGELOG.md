# Changelog

This file keeps the consolidated release history. Detailed component behavior belongs in the component README files rather than being duplicated here.

## Senda Arugus Agent Studio v0.5.4

### Added / changed

- Workflow list cards now use the same visual style as Runtime cards.
- Added per-Workflow **Trace** and **Logs** actions.
- Workflow Trace can be inspected per Workflow Step using the recorded `agent_run_id` when available.
- Workflow Logs can be inspected for the Agent Runtimes used by the Workflow.
- Added accordion-style **Execution Details** for Steps and Final Result.
- Added Japanese / English i18n strings for Workflow Trace, Logs, and Execution Details.

### Verification

- Agent Studio test suite: 29 passed.

---

## Senda Arugus Agent Studio v0.5.3

- Split Workflow list and Workflow registration into separate WebUI views.
- Added dedicated **Workflows** and **Workflow registration** navigation.
- Workflow list focuses on registered definitions, lifecycle controls, and latest execution state.
- Workflow registration retains **Register only** and **Register & Run**.
- After registration, the UI returns to the Workflow list.
- Added Japanese / English i18n for the new navigation and views.

---

## Senda Arugus Agent Studio v0.5.2

### Added

- Runtime registration: **Register only** / **Register & Run**.
- Workflow registration: **Register only** / **Register & Run**.
- Workflow Start / Stop / Delete and existing Approval controls.
- Re-run existing registered Workflow definitions.
- Workflow start resets current execution Steps / Result while preserving the definition.
- Workflow stop cancels the Supervisor task and stops the active one-shot Agent when possible.
- MCP control surface additions: `register_workflow`, `run_workflow`, `stop_workflow`, `delete_workflow`.

### API

```text
POST   /api/agents                          # start_immediately=true|false
POST   /api/workflows                       # start_immediately=true|false
POST   /api/workflows/{workflow_id}/start
POST   /api/workflows/{workflow_id}/stop
DELETE /api/workflows/{workflow_id}
```

### Verification

- Agent Studio test suite: 29 passed.

---

## Senda Arugus Agent Studio v0.5.1

### Added

- Japanese / English language selector.
- Browser language detection on first visit.
- Language persistence in browser `localStorage`.
- Lightweight catalog in `app/static/i18n.js`.
- Localization for UI actions, help text, validation, confirmations, Workflow messages, Trace, and Logs.

### Translation policy

- Senda product names remain in English.
- Common technical terms such as Agent, Runtime, Hook, Workflow, Supervisor, Goal, LLM, MCP, RAG, Jev, Docker, JSON, API, Trace, and Logs remain in English where clearer.

---

## Senda Arugus Agent Studio v0.5.0

### Added

- Built-in `senda-supervisor` control-plane identity.
- Supervisor backends: `llm`, `jev`, `deterministic`.
- TypeSafe Jev / System One routing via `typesafe-sdk`.
- Jev Agent choice over callable Agent IDs plus a `finish` outcome.
- Jev telemetry: selected Agent, probabilities, confidence, model, request ID, latency, and usage when available.
- Supervisor-generated events published through the Studio event bus and forwarded upstream when configured.
- Event families:
  - `supervisor.decision.*`
  - `orchestrator.jev.*`
  - `orchestrator.llm.*`
  - existing `orchestrator.plan.*` retained for compatibility
- Optional Jev confidence threshold and LLM fallback.

### Workflow model

- `Goal` is the Supervisor prompt.
- `Allowed Agents` is a candidate set only; order does not define execution order.
- The Supervisor normally chooses the first worker.
- `entry_agent_id` remains only as an advanced first-worker override for tests / backward compatibility.

---

## Senda Arugus Agent Studio v0.4.0

### Added

- Agent Registry metadata for routing: description, capabilities, tags, input/output schema, risk, approval, allowed callers.
- One-shot child Agent execution from registered Runtime templates.
- `SENDA_AGENT_INPUT`, Workflow identity, and Argus run correlation environment injection.
- Structured Agent result marker: `[senda-agent-result] {...}`.
- Persistent Workflow / Workflow Step state.
- LLM Supervisor loop with `max_steps`, Allowed Agent validation, and structured decisions.
- Human approval gate.
- Workflow UI and `workflow.*` / `orchestrator.*` trace events.
- stdio MCP control server for Agent and Workflow operations.
- Deterministic local planner and demo workers for offline tests.

### Control boundary

The LLM does not receive Docker access. It selects only registered Agent IDs; Agent Studio validates and executes the selection through the managed Runtime boundary.

---

## Earlier Hook / Runtime releases

### Node Zero-code v0.9.0

- Node zero-code bootstrap and ESM loader.
- Automatic supported provider / MCP interception for ESM and CommonJS.
- Linux process / systemd discovery and installation workflow.
- Direct Argus HTTP exporter with fail-open behavior.

### Python Zero-code v0.8.0

- Python / venv discovery.
- Offline wheel installation.
- `.pth` startup hook without Agent source changes.
- Global / user `hooks.env` configuration.
- Bulk install/uninstall, status, rollback, and restart-required PID reporting.

### Docker Runtime

- Shared Python and Node Hook-enabled Runtime images.
- Python `.pth` bootstrap and Node `NODE_OPTIONS` preload.
- Docker Compose examples and smoke tests.

### v0.7.0

- LangChain JS, LangGraph, LlamaIndex TS, Vercel AI SDK, and OpenAI Agents SDK integrations.
- Stable `trace_id` handling improvements.

### v0.6.0

- Initial Senda-Argus Hooks JS support with event schema 0.2, redaction, exporters, and provider / MCP hooks.
