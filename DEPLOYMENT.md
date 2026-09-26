# Senda-Argus Deployment Guide

This guide covers the current deployment patterns for Senda-Argus Hooks and Senda Argus Agent Studio.

## Deployment modes

```text
1. Hook-enabled Docker Runtime managed by Agent Studio
2. Existing Python Agent zero-code / auto-hook
3. Existing Node.js Agent zero-code preload
```

For new Agent Studio and Multi-Agent Workflow evaluation, use the Docker Runtime path first.

## Quick install

From the repository root:

```bash
./scripts/install.sh
```

This verifies Docker, builds `senda/python-agent:0.8`, and starts Agent Studio.

Build the Node Runtime too:

```bash
./scripts/install.sh --with-node
```

Status:

```bash
./scripts/status.sh
```

Uninstall Agent Studio while preserving Runtime containers/images by default:

```bash
./scripts/uninstall.sh
```

## Hook-enabled Docker Runtime

Build the common Python Runtime:

```bash
docker build \
  -f docker/python/Dockerfile \
  -t senda/python-agent:0.8 \
  .
```

The image includes Senda-Argus Hooks, startup bootstrap, environment-based configuration, and fail-open behavior.

A host Agent can be mounted without building an Agent-specific image:

```bash
docker run --rm \
  --name my-agent \
  -v /path/to/my-agent:/workspace \
  -w /workspace \
  -e SENDA_AGENT_WORKSPACE=/workspace \
  -e SENDA_AGENT_INSTALL_DEPS=true \
  senda/python-agent:0.8 \
  python agent.py
```

When `SENDA_AGENT_INSTALL_DEPS=true`, `/workspace/requirements.txt` is installed when present.

## Senda Argus Agent Studio

Start from the Agent Studio directory:

```bash
cd agent-studio
docker compose up -d --build
```

Open:

```text
http://localhost:8080
```

Agent Studio needs access to the Docker Engine. In the current MVP this is typically provided by mounting the Docker socket into the Studio container.

Only containers labeled as Senda Agent Runtime are intended to be managed.

### Runtime registration

The WebUI separates Runtime list/operations from Runtime registration.

Runtime registration supports:

```text
Register only
Register & Run
```

Recommended restart policy:

```text
long-running Agent: unless-stopped
one-shot / test Agent: no
failure retry: on-failure
```

`Maximum Retry Count` is used only with `on-failure`.

### Runtime operations

Registered Runtimes support:

```text
Start
Stop
Restart
Trace
Logs
Delete
```

Trace and Docker Logs are shown inside the selected Runtime card.

### Multi-Agent Workflow registration

The WebUI separates **Workflows** and **Workflow registration**.

Workflow registration supports:

```text
Register only
Register & Run
```

A Workflow definition includes:

```text
Goal
Allowed Agents
Initial Input JSON
Max Steps
Supervisor Mode
Supervisor Model / Base URL where applicable
Jev Confidence Threshold / Fallback where applicable
```

`Allowed Agents` is a candidate set. Its order is not an execution order.

The built-in `senda-supervisor` selects the next worker Agent using one of:

```text
llm
jev
deterministic
```

The advanced first-worker override should normally remain empty so the Supervisor chooses the first worker.

### Workflow operations

Registered Workflows support:

```text
Start
Stop
Trace
Logs
Delete
Approve     # only when approval is required
```

Starting an existing Workflow resets its current Steps and Final Result while keeping its registered definition.

Stopping a Workflow cancels the Supervisor task and attempts to stop the currently running one-shot Agent container.

The Workflow list includes an **Execution Details** accordion containing Steps and Final Result. Trace and Logs are shown inside the selected Workflow card in the same style as Runtime cards.

### One-shot child Agent execution

A registered Runtime acts as the execution template. Workflow execution creates a one-shot child container and forces:

```text
restart=no
```

The child receives execution context such as:

```text
SENDA_AGENT_INPUT
SENDA_AGENT_RUN_ID
SENDA_ARGUS_RUN_ID
SENDA_WORKFLOW_RUN_ID
SENDA_PARENT_AGENT_ID
```

A worker Agent can return a structured result with:

```text
[senda-agent-result] {"key":"value"}
```

### Jev / TypeSafe configuration

Jev is optional and runs from the Agent Studio container through `typesafe-sdk`.

Worker Runtime images do not need the TypeSafe SDK.

Example environment:

```bash
export TYPESAFE_API_KEY='...'
export TYPESAFE_BASE_URL='https://api.typesafe.ai'   # optional
export TYPESAFE_DEFAULT_MODEL='jev-latest'           # optional

docker compose up -d --build
```

Optional Studio settings include Jev timeout, confidence threshold, and fallback behavior.

Jev decision telemetry can include selected Agent, probabilities, confidence, model, request ID, latency, and usage when supplied by the backend.

### Forward Studio events to Senda-Argus

When `SENDA_STUDIO_ARGUS_UPSTREAM` is configured, Agent Studio forwards Studio-generated events, including Supervisor / Workflow / Jev events, to the upstream Senda-Argus endpoint.

Runtime Hook event ingestion endpoint:

```text
/v1/agent-runs/ingest
```

For transport troubleshooting, enable or inspect:

```text
[senda-argus-http]
[senda-studio-argus]
```

## Existing Python Agent auto-hook

Install the Python package into the same environment as the existing Agent:

```bash
python -m pip install ./python
senda-hooks autohook install
```

Status:

```bash
senda-hooks autohook status
```

Uninstall:

```bash
senda-hooks autohook uninstall
```

No Agent source import or `register(...)` change is required.

## Existing Node.js Agent zero-code preload

Recommended Node versions:

```text
Node 20 / 22 LTS
Node 18.19+ for the ESM loader path
```

Use the Node zero-code installer documented under `tools/` to scan, install, inspect status, and uninstall the managed preload.

Supported zero-code interception includes supported OpenAI, Anthropic, Ollama, MCP, and OpenAI Agents provider layers.

## Common runtime configuration

```text
SENDA_ARGUS_ENABLED=true
SENDA_ARGUS_EXPORTER=argus
SENDA_ARGUS_ENDPOINT=https://argus.example.local
SENDA_ARGUS_API_KEY=...
SENDA_ARGUS_PROJECT=my-agent
SENDA_ARGUS_ENVIRONMENT=prod
SENDA_ARGUS_CAPTURE_PROMPT=false
SENDA_ARGUS_CAPTURE_RESPONSE=false
SENDA_ARGUS_CAPTURE_ARGUMENTS=false
SENDA_ARGUS_CAPTURE_RESULT=false
SENDA_ARGUS_CAPTURE_HASH=true
SENDA_ARGUS_REDACT=true
```

## Security notes

- Keep raw prompt / response capture disabled unless required.
- Keep redaction enabled.
- Store API keys in protected files or secret-management systems.
- Treat traces, Workflow results, and Docker logs as security-sensitive.
- Docker Engine control is highly privileged; restrict access to Agent Studio accordingly.
- The Supervisor does not receive direct Docker access. It selects registered Agent IDs and Agent Studio validates/executes the choice.
