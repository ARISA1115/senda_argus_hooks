# Senda Agent Studio Release Notes

## v0.5.4

- Updated the **Workflows** list UI to match the Runtime card style more closely.
- Added per-Workflow **Trace** and **Logs** actions in each Workflow card.
- Workflow Trace now lets you inspect traces per execution step using the recorded `agent_run_id` when available.
- Workflow Logs now lets you inspect Docker logs for the Runtime containers used by that Workflow.
- Added accordion-style **Execution Details** so steps and final result can be expanded/collapsed without making the list view too tall.
- Added Japanese/English i18n strings for the new Workflow Trace / Logs / Execution Details controls.

## v0.5.3

- Split Multi-Agent Workflow list and registration into separate Web UI views.
- Added a dedicated **Workflow登録 / Register Workflow** navigation item.
- The **Workflows** view now focuses on registered Workflow lifecycle controls and execution state.
- The Workflow registration view keeps **登録のみ / Register only** and **登録 & 実行 / Register & Run** actions.
- After successful Workflow registration, the UI returns to the Workflow list.
- Added Japanese/English i18n strings for the new Workflow list/registration navigation.

## v0.5.2

Runtime / Workflow lifecycle controls.

### Added

- Runtime registration now supports **Register only** and **Register & Run**.
- Multi-Agent Workflow registration now supports **Register only** and **Register & Run**.
- Registered Workflows have Start, Stop, Delete, and existing Approval controls in the Workflow card.
- A completed/stopped Workflow can be started again from the registered definition.
- Workflow start resets the current execution steps/result while keeping the registered Goal, Agent candidate set, Supervisor configuration, and Initial Input.
- Stopping a Workflow cancels its Supervisor task and stops the currently running one-shot Agent container when present.
- MCP control surface adds `register_workflow`, `run_workflow`, `stop_workflow`, and `delete_workflow`.

### API

- `POST /api/agents` adds `start_immediately` (default `true`).
- `POST /api/workflows` adds `start_immediately` (default `true`).
- `POST /api/workflows/{workflow_id}/start`
- `POST /api/workflows/{workflow_id}/stop`
- `DELETE /api/workflows/{workflow_id}`

### Tests

- Added register-only Runtime coverage.
- Added Workflow register/reset/delete lifecycle coverage.
- Full Agent Studio suite: 29 tests.

## v0.5.1

Web UI i18n update.

### Added

- Japanese / English language switcher in the Agent Studio header.
- Browser-language detection on first visit and persistent selection in `localStorage`.
- Lightweight frontend i18n catalog in `app/static/i18n.js`; no additional runtime dependency or frontend framework is required.
- Static UI labels, helper text, placeholders, confirmation dialogs, workflow messages, Runtime operations, Trace labels, and Docker Logs controls now use the locale catalog.

### Translation policy

- Senda product names stay in English.
- Common technical terms such as Agent, Runtime, Hook, Workflow, Supervisor, Goal, LLM, MCP, RAG, Jev, Docker, JSON, API, Trace, Logs, Project, and Environment remain in English where translating them would reduce clarity.
- Japanese mode translates surrounding explanations and actions rather than forcing every technical term into Japanese.

## v0.5.0

Supervisor/Jev control-plane update.

### Added

- Explicit built-in `senda-supervisor` control-plane identity. The Supervisor is not a worker Runtime and does not receive Docker access.
- Supervisor backends: `llm`, `jev`, and `deterministic`.
- TypeSafe Jev/System One routing via the official Python `typesafe-sdk`.
- Jev Choice decisions across the currently callable Agent IDs plus a `finish` outcome.
- Jev telemetry captured in Argus events: selected Agent, per-choice probabilities, confidence, model, request ID, latency and token usage when available.
- Supervisor events are now published through the same Studio event bus as runtime Hook events, so internal workflow/Jev decisions appear in Live Hook Events and are forwarded to `SENDA_STUDIO_ARGUS_UPSTREAM` when configured.
- New events: `supervisor.decision.*`, `orchestrator.jev.*`, and `orchestrator.llm.*`. Existing `orchestrator.plan.*` events remain for compatibility.
- Optional Jev confidence threshold and LLM fallback.

### Workflow model clarification

- `Goal` is the orchestration prompt presented to the Supervisor.
- `Allowed Agents` is only a candidate set; list order does not define execution order.
- The normal first step is a Supervisor decision.
- The former **Entry Agent** UI is now an advanced **First Worker Override** for deterministic tests/backward compatibility. The API field `entry_agent_id` is retained so existing clients continue to work.

### Docker / configuration

The Agent Studio image now includes `typesafe-sdk`. Jev credentials/configuration stay in the Studio container rather than worker Agent images.

```bash
export TYPESAFE_API_KEY='...'
export TYPESAFE_BASE_URL='https://api.typesafe.ai'   # optional
export TYPESAFE_DEFAULT_MODEL='jev-latest'           # optional
docker compose up -d --build
```

### Tests

- Added Jev supervisor routing/telemetry tests.
- Full Agent Studio suite: 24 tests.

## v0.4.0

Multi-Agent orchestration control-plane update.

### Added

- Agent Registry metadata for LLM routing: description, capabilities, tags, input/output schema, risk level, approval requirement and allowed callers.
- One-shot child Agent execution API derived from a registered Runtime template.
- `SENDA_AGENT_INPUT`, workflow identity and Argus run correlation environment injection.
- Structured Agent result marker support (`[senda-agent-result] {...}`).
- Persistent workflow / workflow-step state in Studio SQLite.
- LLM supervisor loop with bounded `max_steps`, Agent allow-listing and structured JSON decisions.
- Human approval gate for approval-required Agents.
- Workflow UI and trace events (`workflow.*`, `orchestrator.*`).
- stdio MCP control server with Agent and workflow tools.
- Deterministic local planner mode for control-plane smoke tests.
- Three deterministic demo workers under `demo-agents/`.

### Safety / control boundary

The LLM does not receive Docker access. It selects only registered Agent IDs; Agent Studio validates the selection and performs container execution through the existing managed-runtime boundary.

### Tests

- Agent Studio test suite expanded to cover Agent profiles, workflow persistence, structured result parsing, child-run environment injection and deterministic multi-Agent orchestration.

## v0.3.0

Runtime workspace UI update.

### Changed

- Split the WebUI into two explicit views:
  - **Runtimes** for execution, monitoring, and operations.
  - **Register Runtime** for creating/deploying a new Senda Agent Runtime.
- Removed the always-visible registration form from the runtime operations screen.
- Moved Trace and Docker Logs from global bottom-of-page panels into each Runtime card.
- Added per-Runtime accordion-style Trace and Docker Logs panels so users can immediately identify which Runtime owns the data.
- Runtime cards keep Start / Stop / Restart / Trace / Logs / Delete actions together.
- Trace groups and rich Trace detail now render inside the selected Runtime card.
- Docker Logs now render inside the selected Runtime card and include an inline refresh action.
- Live Hook Events remain a global runtime-monitoring stream on the Runtimes view.
- Added simple hash navigation (`#runtimes`, `#register`) without introducing a frontend framework.

### Notes

Hook events are generated when the Agent actually executes. If an existing trace only contains an older `llm.request`, restart or re-run the Runtime after updating the Hook SDK/runtime to generate a new trace.

### Tests

- Existing API/store/runtime tests remain compatible.
- WebUI JavaScript syntax is validated in the release build.

# Senda Agent Studio Release Notes

## v0.2.0

Trace observability update.

### Added

- Rich Trace Detail API: `GET /api/traces/detail`
  - model
  - prompt/input when present in Hook event payloads
  - response/output when present in Hook event payloads
  - latency/duration when present, otherwise derived from first/last event timestamp
  - trace/run status and event count
- Graphical trace flow in the WebUI.
  - Agent events
  - LLM request
  - LLM response/completion
  - MCP tool-call requested
  - MCP tool-call completed/failed
  - generic tool-call and retrieval events
- Prompt/Input and Response/Output detail panes.
- Model and latency badges in Live Hook Events.
- Docker log viewer for Senda Agent Runtime containers.
  - `GET /api/agents/{container_id}/logs`
  - validates the Senda Runtime management label before reading logs
  - supports Docker multiplexed stdout/stderr streams
- `Logs` action in the Runtime list.

### Compatibility note

Agent Studio can now display `llm.response` / `llm.completed` events and MCP flows when those events are emitted by Senda Hooks. Agent Studio itself does not fabricate a response event when the Hook only sends `llm.request`; response-side event emission must be implemented in the Hook SDK/runtime instrumentation.

### Tests

- 12 tests passing.
- Python compile check passed.
- WebUI JavaScript syntax check passed.

## v0.1.3

- Added safe Senda Runtime deletion from the UI.
