# MCP / RAG test agents and Argus HTTP diagnostics

This change adds transport-level diagnostics plus two deterministic Agent Studio test workloads.

## 1. Argus HTTP transport log

Agent Studio-created Python runtimes now receive:

```text
SENDA_ARGUS_HTTP_LOG=true
```

When an event batch is sent, Docker Logs show transport metadata only:

```text
[senda-argus-http] POST http://.../v1/agent-runs/ingest events=1 bytes=1234
[senda-argus-http] POST completed status=200 url=http://.../v1/agent-runs/ingest events=1
```

Failures show:

```text
[senda-argus-http] POST failed url=http://.../v1/agent-runs/ingest events=1 error=...
```

API keys, HTTP headers, prompts, responses, and event bodies are not printed.

To disable it for a runtime, override the Runtime environment with:

```json
{"SENDA_ARGUS_HTTP_LOG":"false"}
```

## 2. Agent Studio -> upstream Argus log

When `SENDA_STUDIO_ARGUS_UPSTREAM` is configured, the Studio container log also records forwarding start/success/failure using the prefix:

```text
[senda-studio-argus]
```

## 3. MCP test

Register `test-mcp-agent/` as a Python runtime.

Suggested values:

```text
Name: test-mcp-agent
Runtime Image: senda/python-agent:0.8
Host Agent Path: <absolute path>/test-mcp-agent
Container Path: /workspace
Entrypoint: agent.py
Install requirements: enabled
```

The workload uses a real Python MCP stdio server/client pair and calls three deterministic tools.  No external service or API key is required.

## 4. RAG test

Register `test-rag-agent/` using the same Runtime settings and its own absolute Host Agent Path.

The workload has no external dependencies and uses local deterministic documents.  It exercises embedding, retrieval, and RAG query hooks.

## 5. What to verify in the Web UI

For each runtime:

1. Open **Trace** and confirm MCP or RAG lifecycle events.
2. Open **Logs** and confirm `[senda-argus-http] POST ...` followed by `status=200`.
3. If Studio forwards to a real Argus instance, check the Studio container logs for `[senda-studio-argus] ... status=200`.
