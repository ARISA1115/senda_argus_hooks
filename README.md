# Senda-Argus Hooks

Senda-Argus Hooks is a hook-based observability SDK for AI Agents, LLM SDKs, MCP, agent frameworks, and RAG workflows.

It collects normalized execution events from runtime hooks, monkey patches, callback integrations, and framework wrappers without requiring application-level audit calls or business-logic changes in Agent applications. The events can be exported to Senda-Argus for analysis, correlation, risk scoring, alerting, and visualization.

> **Patent Notice**  
> Certain concepts and techniques related to Senda-Argus, including AI Agent execution trace collection, decision trace reconstruction, and runtime audit event correlation, are patent pending in Japan. This notice does not change the Apache License 2.0 terms applicable to this repository.

## Highlights

- Python and Node.js / TypeScript Hook support
- Existing Python Agent auto-hook / zero-code deployment
- Existing Node Agent zero-code preload
- Hook-enabled Docker Runtime images
- OpenAI / Anthropic / LiteLLM / Ollama hooks
- MCP lifecycle hooks
- OpenAI Agents, LangChain, LangGraph, LlamaIndex / RAG integrations
- JSONL / stdout / Parquet / Argus exporters
- Redaction and capture controls
- Senda Arugus Agent Studio for Runtime, Trace, Logs, and Multi-Agent Workflow management

## Repository layout

```text
senda-argus-hooks/
├─ python/          # Python SDK and integrations
├─ js/              # Node.js / TypeScript SDK and integrations
├─ browser/         # Browser hook package
├─ docker/          # Hook-enabled Python / Node runtimes
├─ agent-studio/    # Senda Arugus Agent Studio WebUI / control plane
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

## Event model

Common fields include:

```text
schema_version
 event_id
 trace_id
 span_id
 parent_span_id
 timestamp
 project
 environment
 event_type
 source
 actor
 data
 security
 status
 latency_ms
 error
```

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
workflow.*
supervisor.*
orchestrator.*
```

## Senda Arugus Agent Studio

`agent-studio/` provides the Docker-based control plane and observability UI.

Current capabilities through **v0.5.4**:

### Runtime management

- Runtime list and dedicated Runtime registration view
- **Register only** / **Register & Run** lifecycle
- Start / Stop / Restart / Delete
- Docker restart policy selection: `no`, `on-failure`, `always`, `unless-stopped`
- Maximum retry count for `on-failure`
- Generic host Agent directory mount into a shared Hook-enabled Runtime
- Per-Runtime Trace accordion
- Per-Runtime Docker Logs accordion
- Global Live Hook Events

### Multi-Agent Workflow control plane

- Built-in `senda-supervisor`
- Supervisor modes: `llm`, `jev`, `deterministic`
- Agent Registry metadata for routing:
  - Description
  - Capabilities
  - Tags
  - Input / Output JSON Schema
  - Risk level
  - Approval requirement
  - Allowed callers
- Goal-based Agent selection
- `Allowed Agents` as a candidate set; list order does not define execution order
- One-shot child Agent execution with `restart=no`
- Structured Agent result contract using `[senda-agent-result] {...}`
- Persistent Workflow and Step state
- Human approval gate
- Workflow Start / Stop / Delete / re-run
- Workflow **Register only** / **Register & Run** lifecycle
- Separate **Workflows** and **Workflow registration** views
- Per-Workflow Trace and Logs panels
- Step-based Workflow Trace selection
- Workflow Execution Details accordion for Steps and Final Result

### Jev / TypeSafe integration

Jev is available as an optional Supervisor decision backend in Agent Studio.

- `typesafe-sdk` is installed only in Agent Studio, not in worker Agent images
- Agent choice, confidence, and choice probabilities are recorded when available
- Jev telemetry can be forwarded to Senda-Argus together with other Studio events
- Optional confidence threshold and LLM fallback

Relevant event families include:

```text
supervisor.decision.*
orchestrator.jev.*
orchestrator.llm.*
workflow.*
```

### i18n

The WebUI supports Japanese and English.

Technical terms and Senda product names remain in English when translating them would reduce clarity. The selected language is stored in browser `localStorage`.

See [`agent-studio/README.md`](./agent-studio/README.md) for detailed usage and control-plane behavior.

## Quick setup

```bash
./scripts/install.sh
```

This builds the common Python Hook Runtime and starts Agent Studio.

To build the Node Runtime too:

```bash
./scripts/install.sh --with-node
```

Create a Runtime from an existing host Agent directory:

```bash
./scripts/runtime-create.sh \
  --name my-agent \
  --host-path /path/to/my-agent
```

See [DEPLOYMENT.md](./DEPLOYMENT.md) for deployment and operational details.

## Privacy and security defaults

Production deployments should keep raw content capture disabled unless explicitly required.

```text
SENDA_ARGUS_CAPTURE_PROMPT=false
SENDA_ARGUS_CAPTURE_RESPONSE=false
SENDA_ARGUS_CAPTURE_ARGUMENTS=false
SENDA_ARGUS_CAPTURE_RESULT=false
SENDA_ARGUS_CAPTURE_HASH=true
SENDA_ARGUS_REDACT=true
```

Exported events, Workflow traces, and Runtime logs are security-sensitive data. Do not commit credentials or sensitive outputs to Git.

## Documentation

- [Deployment and operations](./DEPLOYMENT.md)
- [Release history](./CHANGELOG.md)
- `python/README.md` — Python SDK details
- `js/README.md` — Node.js / TypeScript SDK details
- `agent-studio/README.md` — Agent Studio usage
- `docker/README.md` — Runtime image details
- `TEST_AGENTS.md` — deterministic MCP / RAG validation workloads

## License

Apache License 2.0. See [LICENSE](./LICENSE).
