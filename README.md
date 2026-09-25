# Senda-Argus Hooks

Senda-Argus Hooks is a hook-based observability SDK for AI agents, LLM SDKs, MCP, agent frameworks, and RAG workflows.

It collects normalized execution events from runtime hooks, monkey patches, callback integrations, and framework wrappers without requiring application-level `audit.event()` calls or business-logic changes in agent applications.

The collected events are intended for downstream analysis, correlation, risk scoring, alerting, and visualization by systems such as Senda-Argus.

> **Patent Notice**  
> Certain concepts and techniques related to Senda-Argus, including AI agent execution trace collection, decision trace reconstruction, and runtime audit event correlation, are patent pending in Japan. This notice does not change the Apache License 2.0 terms applicable to this repository.

## Highlights

- Hook-only observability; no required application audit calls
- Python and Node.js / TypeScript support
- Existing Python Agent auto-hook without source changes
- Existing Node Agent zero-code preload without source changes
- Hook-enabled Docker Runtime images
- Senda Agent Studio for Docker-based Agent Runtime management and trace visualization
- OpenAI / Anthropic / LiteLLM / Ollama hooks
- MCP request / completion / failure lifecycle events
- OpenAI Agents, LangChain, LangGraph, LlamaIndex / RAG integrations
- JSONL / stdout / Parquet / Argus exporters
- Redaction and capture controls
- Stable correlation identifiers such as `agent_id`, `purpose_id`, and `mcp_profile_id`

## Repository layout

```text
senda-argus-hooks/
├─ python/          # Python SDK and integrations
├─ js/              # Node.js / TypeScript SDK and integrations
├─ browser/         # Browser hook package
├─ docker/          # Hook-enabled Python / Node runtimes
├─ agent-studio/    # Senda Agent Studio WebUI
├─ tools/           # Zero-code deployment tools
├─ scripts/         # Docker install / status / runtime-create helpers
├─ README.md
├─ DEPLOYMENT.md
├─ CHANGELOG.md
└─ LICENSE
```

## Supported hook targets

| Target | Status | Typical events |
|---|---|---|
| OpenAI SDK | Experimental | `llm.request`, `llm.error` |
| Anthropic SDK | Experimental | `llm.request`, `llm.error` |
| LiteLLM | Experimental | `llm.request`, `llm.error` |
| Ollama Python SDK | Experimental | `llm.request`, `llm.error` |
| MCP Python SDK | Experimental | `mcp.tool_call.requested`, `mcp.tool_call.completed`, `mcp.tool_call.failed` |
| OpenAI Agents SDK | Experimental | `agent.run.*`, `agent.step.*`, `tool_call.*`, `llm.*` |
| LangChain | Experimental | `llm.*`, `tool_call.*`, `agent.step.*`, `agent.decision` |
| LangGraph | Experimental | `agent.run.*`, `agent.step.*` |
| LlamaIndex / RAG | Experimental | `retrieval.*`, `embedding.*`, `rag.query.*` |
| Node.js provider / MCP hooks | Experimental | `llm.*`, `mcp.tool_call.*` |
| Langflow integration | Example | `llm.*`, `tool_call.*`, `mcp.tool_call.*`, `retrieval.*` |

## Event model

Common event fields include:

- `schema_version`
- `event_id`
- `trace_id`
- `span_id`
- `parent_span_id`
- `timestamp`
- `project`
- `environment`
- `event_type`
- `source`
- `actor`
- `data`
- `security`
- `status`
- `latency_ms`
- `error`

Typical event families:

```text
llm.*
mcp.tool_call.*
tool_call.*
agent.run.*
agent.step.*
agent.decision
retrieval.*
embedding.*
rag.query.*
```

## Quick start: Python SDK

```bash
cd python
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Basic registration:

```python
from senda_argus_hooks import register, shutdown

register(
    project="example-agent",
    environment="dev",
    auto_instrument=True,
    exporters=[{"type": "jsonl", "path": "./logs/events.jsonl"}],
    capture_prompt=False,
    capture_response=False,
    capture_arguments=True,
    capture_result=False,
    redact=True,
)

# Run the normal Agent / LLM / MCP application.

shutdown()
```

For existing Python Agents, use the zero-code deployment path documented in [DEPLOYMENT.md](./DEPLOYMENT.md).

## Quick start: Node.js / TypeScript

```bash
cd js
npm install
npm test
```

Node zero-code deployment can intercept supported provider and MCP layers through `NODE_OPTIONS=--import=...` without adding imports or `register()` calls to the application source. See [DEPLOYMENT.md](./DEPLOYMENT.md).

## Senda Agent Studio

`agent-studio/` provides a WebUI for managing **Senda Agent Runtime** containers and visualizing Hook events.

Current capabilities include:

- Runtime list
- Start / Stop / Restart / Delete
- Runtime registration
- Generic host Agent directory mount into a Hook-enabled Runtime
- Live Hook events
- Agent trace visualization
- Docker logs per Runtime

A typical deployment uses a common Runtime image such as:

```text
senda/python-agent:0.8
```

and mounts an existing host-side Agent directory into `/workspace`, avoiding an Agent-specific image build.

## One-command Docker setup

For a quick local deployment with Docker Desktop or Docker Engine:

```bash
./scripts/install.sh
```

This builds the common Python Hook Runtime and starts Senda Agent Studio. Build the Node Runtime too with:

```bash
./scripts/install.sh --with-node
```

Create a Runtime directly from an existing host Agent directory without using the WebUI:

```bash
./scripts/runtime-create.sh \
  --name my-agent \
  --host-path /path/to/my-agent
```

Check the installation with `./scripts/status.sh`. See [DEPLOYMENT.md](./DEPLOYMENT.md) for removal and advanced options.

## Docker Runtime

The Docker runtime preinstalls Senda-Argus Hooks and supports automatic startup instrumentation.

Python uses a startup bootstrap based on `.pth`; Node uses a preload path through `NODE_OPTIONS`.

See [DEPLOYMENT.md](./DEPLOYMENT.md) for build, runtime, and Agent Studio deployment instructions.

## Privacy and security defaults

Production deployments should keep raw content capture disabled unless explicitly required.

Recommended defaults:

```text
SENDA_ARGUS_CAPTURE_PROMPT=false
SENDA_ARGUS_CAPTURE_RESPONSE=false
SENDA_ARGUS_CAPTURE_ARGUMENTS=false
SENDA_ARGUS_CAPTURE_RESULT=false
SENDA_ARGUS_CAPTURE_HASH=true
SENDA_ARGUS_REDACT=true
```

Exported events are security-relevant audit data. Do not commit runtime logs, API keys, or sensitive exported payloads to public repositories.

## Development

Python:

```bash
cd python
python -m pip install -e "[dev]"
pytest -q -rs
```

Node.js:

```bash
cd js
npm install
npm test
```

## Documentation

- [Deployment and operations](./DEPLOYMENT.md)
- [Release history](./CHANGELOG.md)
- `python/README.md` — Python SDK details
- `js/README.md` — Node.js / TypeScript SDK details
- `agent-studio/README.md` — Agent Studio usage
- `docker/README.md` — Runtime image details

## License

Apache License 2.0. See [LICENSE](./LICENSE).
