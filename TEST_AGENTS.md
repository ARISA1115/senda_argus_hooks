# Agent Studio validation workloads

This file documents the deterministic test workloads used to validate MCP, RAG, transport diagnostics, and basic Agent Studio behavior.

These workloads are for validation only; they are not production Agents.

## Common Runtime settings

Use the shared Python Runtime:

```text
Runtime Image: senda/python-agent:0.8
Container Path: /workspace
Entrypoint: agent.py
Install requirements: enabled
Restart Policy: no
```

`restart=no` is important because each test Agent is a one-shot workload that exits after the test completes.

## Argus HTTP transport diagnostics

Agent Studio-created Python Runtimes receive:

```text
SENDA_ARGUS_HTTP_LOG=true
```

Expected Docker Logs:

```text
[senda-argus-http] POST http://.../v1/agent-runs/ingest events=1 bytes=1234
[senda-argus-http] POST completed status=200 url=http://.../v1/agent-runs/ingest events=1
```

Failures use:

```text
[senda-argus-http] POST failed url=http://.../v1/agent-runs/ingest events=1 error=...
```

Transport diagnostics do not print API keys, HTTP headers, prompts, responses, or event bodies.

Disable per Runtime with:

```json
{"SENDA_ARGUS_HTTP_LOG":"false"}
```

## Upstream Senda-Argus diagnostics

When `SENDA_STUDIO_ARGUS_UPSTREAM` is configured, Studio forwarding logs use:

```text
[senda-studio-argus]
```

Use these logs to distinguish:

```text
Agent -> Agent Studio ingestion
Agent Studio -> upstream Senda-Argus forwarding
```

## MCP test Agent

Register `test-mcp-agent/` as a Python Runtime.

The test launches a local MCP stdio server/client pair and exercises deterministic MCP tools. No external service or API key is required.

Verify:

- `mcp.list_tools.*`
- `mcp.tool_call.requested`
- `mcp.tool_call.completed`
- `POST completed status=200`
- Runtime exits normally after one execution

## RAG test Agent

Register `test-rag-agent/` with the same Runtime settings and its own Host Agent Path.

The workload uses deterministic local documents and local test embeddings. It validates:

- embedding instrumentation
- retrieval instrumentation
- RAG query instrumentation
- Argus event delivery

Verify in Trace / Live Hook Events:

```text
embedding.*
retrieval.*
rag.query.*
```

and confirm Docker Logs contain `POST completed status=200`.

## Multi-Agent Workflow smoke test

For control-plane validation, register the deterministic demo workers:

```text
asset-discovery-agent
vulnerability-agent
report-agent
```

Use `Supervisor Mode = deterministic` only to validate Workflow mechanics without an external LLM.

The deterministic mode is not intended to prove semantic Agent selection quality. For routing behavior, test `llm` or `jev` mode separately.

Verify:

- Workflow registration
- Register only / Register & Run
- Start / Stop / Delete
- one-shot worker execution
- Step persistence
- structured result handoff
- Workflow Trace
- Workflow Logs
- Execution Details accordion
- Final Result

## WebUI checks

For Runtime validation:

1. Open **Trace** inside the Runtime card.
2. Open **Logs** and confirm HTTP transport diagnostics.
3. Confirm one-shot test Runtimes use `restart=no`.

For Workflow validation:

1. Open **Trace** inside the Workflow card and select a Step.
2. Open **Logs** and select the Agent Runtime used by the Workflow.
3. Expand **Execution Details** to inspect Steps and Final Result.
4. If upstream forwarding is enabled, confirm Studio logs show `[senda-studio-argus] ... status=200`.
