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
