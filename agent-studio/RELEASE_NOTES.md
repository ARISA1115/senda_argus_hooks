# Senda Agent Studio Release Notes

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
