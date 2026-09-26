# Senda Agent Studio

Senda Agent Studio is a Docker management and observability UI dedicated to **Senda Agent Runtime** containers.

## Runtime model

Agent-specific Docker images are not required. Build the shared Hook-enabled Python Runtime once:

```bash
docker build -f docker/python/Dockerfile -t senda/python-agent:0.8 .
```

Keep an existing Agent on the host, for example:

```text
/Users/you/agents/soc-agent/
├── agent.py
└── requirements.txt
```

In Agent Studio create a Runtime with:

```text
Runtime Image:      senda/python-agent:0.8
Host Agent Path:    /Users/you/agents/soc-agent
Container Path:     /workspace
Entrypoint:         agent.py
Install deps:       enabled
```

Agent Studio asks the host Docker Engine to create the equivalent of:

```bash
docker run \
  -v /Users/you/agents/soc-agent:/workspace:rw \
  -w /workspace \
  senda/python-agent:0.8 \
  python agent.py
```

The Runtime image already contains Senda Argus Hooks, so the Agent source does not need to import or register the Hook package.

If `/workspace/requirements.txt` exists and dependency installation is enabled, the Runtime installs it before launching the Agent. A SHA-256 marker avoids reinstalling unchanged requirements on container restart.

## Start Agent Studio

```bash
cd agent-studio
docker compose up -d --build
```

Open:

```text
http://localhost:8080
```

## Web UI language / i18n (v0.5.1)

The Web UI supports Japanese and English from the language selector in the header. On the first visit, Studio uses the browser language (`ja` -> Japanese, otherwise English) and stores the selected language in browser `localStorage`.

The translation catalog is intentionally lightweight and lives in:

```text
app/static/i18n.js
```

Translation policy: Senda product names and common engineering terms such as Agent, Runtime, Hook, Workflow, Supervisor, Goal, LLM, MCP, RAG, Jev, Docker, JSON, API, Trace, and Logs remain in English when that is clearer for Japanese operators. Descriptions, actions, help text, validation messages, and confirmations are localized.

To add a language later, add a catalog entry in `i18n.js`, add its code to `SUPPORTED`, and add an option to `#languageSelect` in `index.html`.


## Registration and execution lifecycle (v0.5.2)

Runtime and Workflow registration are now separate from execution. The Web UI provides two actions when creating either resource:

```text
Register only
Register & Run
```

A Runtime registered without execution creates the managed Docker template container but leaves it stopped/created. It can later be started with the existing Runtime **Start** action.

A Workflow registered without execution is stored with status `registered`. Registered Workflows can be started/re-run, stopped, approved when required, or deleted from the Workflows view. Starting an existing Workflow resets its current steps/result and begins a new execution using the same registered definition.

Workflow lifecycle API:

```text
POST   /api/workflows                         # start_immediately=true|false
POST   /api/workflows/{workflow_id}/start
POST   /api/workflows/{workflow_id}/stop
DELETE /api/workflows/{workflow_id}
```

Runtime registration accepts `start_immediately=true|false` on `POST /api/agents`. Existing API clients remain compatible because the default is `true`.

## macOS / Docker Desktop

Use an absolute macOS host path such as `/Users/you/agents/my-agent`. The bind source is resolved by Docker Desktop's host Docker Engine.

The Agent source directory must be shared/accessible to Docker Desktop. Paths under `/Users` normally work with the standard Docker Desktop configuration.

## Management boundary

New Runtime containers are labeled:

```text
com.senda.agent-runtime=true
com.senda.agent.id=<agent_id>
com.senda.agent.runtime=python|node
```

Agent Studio lists and operates only Senda Agent Runtime containers. Start/Stop/Restart also inspect the target container and reject non-Senda containers.

## Stop/Restart behavior

Stop uses Docker's 3-second graceful shutdown timeout and allows up to 15 seconds for the Docker API response. Restart allows up to 20 seconds. This prevents a successful stop from being shown as a Docker socket timeout on Docker Desktop.

### Runtime deletion

The Agents list includes a **Delete** button. Agent Studio only removes containers that are verified as Senda Agent Runtimes (`com.senda.agent-runtime=true`). If the runtime is running, Studio stops it first. Deletion does **not** remove the common Runtime image or the host Agent source directory mounted into `/workspace`.

## Trace observability (v0.2.0)

Agent Studio renders a trace as a flow instead of only a flat event table. Supported event families include:

```text
Agent -> LLM Request -> Response
Agent -> LLM Request -> MCP -> Tool Result -> LLM Request -> Response
```

The Trace Detail view extracts and displays the following fields when they are present in Hook event payloads:

- model / model_name
- prompt / input / messages
- response / output / content / text / result
- latency_ms / duration_ms / elapsed_ms
- status
- trace_id / run_id

If explicit latency is not present and a trace has multiple events, Studio derives elapsed time from the first and last timestamps.

### Response-side events

Studio recognizes `llm.response`, `llm.completed`, and `llm.completion`. It intentionally does not invent a response event from an `llm.request` event. If the current Hook instrumentor emits only `llm.request`, update the Hook SDK/runtime instrumentor to emit a response-side event as well.

## Docker logs

Each Senda Agent Runtime has a **Logs** action. Studio retrieves only logs for containers that pass the Senda Runtime label check.

API:

```text
GET /api/agents/{container_id}/logs?tail=300
```

The common Runtime image and host Agent source are not modified by log viewing.

## Runtime workspace UI (v0.3.0)

The WebUI separates registration from runtime operations:

```text
Runtimes
  - Runtime list
  - Start / Stop / Restart / Delete
  - Per-Runtime Trace accordion
  - Per-Runtime Docker Logs accordion
  - Global Live Hook Events

Register Runtime
  - Runtime image
  - Host Agent Path
  - Container Path / Entrypoint
  - Project / Environment
  - Restart Policy (`no`, `on-failure`, `always`, `unless-stopped`)
  - Maximum Retry Count (`on-failure` のみ)
  - Environment JSON
```

Trace and Docker Logs are intentionally displayed inside the selected Runtime card rather than in global panels at the bottom of the page. This makes the owning Agent/Runtime explicit when multiple Runtimes are registered.

Use the top navigation or these hashes directly:

```text
http://localhost:8080/#runtimes
http://localhost:8080/#register
```

When validating a Hook change, restart or re-run the Agent Runtime so a new execution generates new Hook events and a new trace. Historical traces are not rewritten by a UI or Hook SDK update.


## Restart policy

Runtime registration lets you select the Docker restart policy from the Web UI. The default remains `unless-stopped` for backward compatibility.

Use `no` for one-shot/test workloads such as `test-mcp-agent` and `test-rag-agent`; use `unless-stopped` for long-running service Agents. `Maximum Retry Count` is sent to Docker only when `on-failure` is selected.

## Multi-Agent orchestration (v0.4.0)

Agent Studio can now act as the control plane for multi-Agent execution. The design keeps LLM planning separate from Docker control:

```text
User / Trigger
    -> LLM Supervisor
    -> Agent Registry metadata
    -> validated Agent Studio run API
    -> one-shot child Agent container
    -> result
    -> LLM Supervisor selects the next Agent
```

The LLM never receives direct Docker access. It selects only from registered `agent_id` values and Agent Studio performs the actual execution.

### Agent Registry metadata

Runtime registration now supports orchestration metadata:

- Description
- Capabilities
- Tags
- Input / Output JSON Schema
- Risk level (`low`, `medium`, `high`)
- Human approval requirement
- Allowed callers

This metadata is stored in Studio SQLite rather than Docker labels so JSON schemas do not need to be compressed into labels.

### One-shot Agent execution contract

A registered Runtime acts as the execution template. `POST /api/agents/{agent_id}/run` creates a one-shot child container with the same image, mount, command and network, but forces:

```text
restart=no
```

The child receives:

```text
SENDA_AGENT_INPUT=<JSON>
SENDA_AGENT_RUN_ID=<agent run id>
SENDA_ARGUS_RUN_ID=<same agent run id>
SENDA_WORKFLOW_RUN_ID=<workflow id>       # when part of a workflow
SENDA_PARENT_AGENT_ID=senda-orchestrator  # when supervisor initiated
```

An Agent may return a structured result by printing one line:

```text
[senda-agent-result] {"key":"value"}
```

If no result marker is emitted, Studio returns the run logs as `result_text`.

Useful APIs:

```text
POST   /api/agents/{agent_id}/run
GET    /api/runs/{run_id}
POST   /api/runs/{run_id}/wait
GET    /api/runs/{run_id}/result
DELETE /api/runs/{run_id}
```

### Supervisor workflow

The **Workflows** view starts an orchestration run. `senda-supervisor` is a Studio control-plane component, not a worker Runtime. It receives the Goal, Initial Input, callable Agent Registry metadata, and previous results, then chooses the next worker or `finish`:

```text
Goal + Agent Registry + workflow state
        -> senda-supervisor
        -> decision backend (llm / jev / deterministic)
        -> run selected worker Agent
        -> collect structured result
        -> senda-supervisor
        -> ...
        -> finish
```

`Allowed Agents` limits the candidate set only; its order is not the execution order. The legacy `entry_agent_id` field is retained as a **First Worker Override** for tests/backward compatibility. Normal workflows should leave it empty so the Supervisor chooses the first worker as well.

The supervisor is bounded by `max_steps` and `allowed_agents`. An Agent with `requires_approval=true` causes the workflow to enter `pending_approval` until the user presses **Approve** or calls the approval API.

Workflow APIs:

```text
POST /api/workflows
GET  /api/workflows
GET  /api/workflows/{workflow_id}
POST /api/workflows/{workflow_id}/approve
```

### Supervisor backends

`llm` uses an OpenAI-compatible `/v1/chat/completions` endpoint:

```bash
export SENDA_STUDIO_LLM_BASE_URL='http://your-llm-endpoint:port'
export SENDA_STUDIO_LLM_MODEL='your-model-name'
export SENDA_STUDIO_LLM_API_KEY='optional-key'
```

`jev` uses TypeSafe System One through the official Python SDK installed in the Agent Studio image:

```bash
export TYPESAFE_API_KEY='...'
export TYPESAFE_BASE_URL='https://api.typesafe.ai'   # optional
export TYPESAFE_DEFAULT_MODEL='jev-latest'           # optional
export SENDA_STUDIO_JEV_CONFIDENCE_THRESHOLD='0'     # 0 disables gating
export SENDA_STUDIO_JEV_FALLBACK='none'              # or llm
```

For Jev routing, Studio creates one `Choice` question whose options are the callable Agent IDs plus `__finish__`. The selected choice, probabilities, confidence, model, request ID and latency are attached to `orchestrator.jev.completed` and `supervisor.decision.completed` events. These internal events use the Studio event bus and are forwarded to the upstream Argus ingest API when `SENDA_STUDIO_ARGUS_UPSTREAM` is configured.

For an offline control-plane smoke test, select `deterministic` Supervisor mode. This verifies Agent execution, result passing and workflow state without an external model.

Rebuild after enabling Jev because `typesafe-sdk` is installed in the Studio image:

```bash
docker compose up -d --build
```

### Agent Studio MCP server

Agent Studio exposes the same control-plane operations through a stdio MCP server:

```bash
docker exec -i \
  -e SENDA_STUDIO_URL=http://127.0.0.1:8080 \
  senda-agent-studio \
  python -m app.mcp_server
```

MCP tools:

```text
list_agents
get_agent
run_agent
get_run
wait_run
get_run_result
start_workflow
get_workflow
approve_workflow
```

The MCP server delegates to the Studio HTTP API and does not manipulate Docker directly.

### Demo workers

`demo-agents/` contains three deterministic workers for local workflow testing. See `demo-agents/README.md`.

### One-command multi-Agent smoke test

After rebuilding Agent Studio and the shared Python Runtime, run from the repository root:

```bash
python3 scripts/multi-agent-smoke.py
```

The script registers the three `demo-agents/` workers if needed, starts a deterministic workflow, and polls until `success` or `failed`. It exercises Agent Registry metadata, one-shot child containers, input/result passing, workflow persistence and supervisor routing without an external LLM.
## Workflow list and registration UI (v0.5.3)

The Web UI separates Workflow lifecycle management from Workflow registration, matching the Runtime UI model:

- **Workflows**: list registered Workflows, inspect status/steps/results, and Start/Stop/Delete them.
- **Workflow登録 / Register Workflow**: configure a new Workflow and choose **登録のみ / Register only** or **登録 & 実行 / Register & Run**.

After a Workflow is registered successfully, the UI returns to the Workflows list.

