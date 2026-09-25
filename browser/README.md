# Senda-Argus Browser Hooks

Senda-Argus Browser Hooks is a browser-side, hook-only observability SDK for LLM and MCP communication initiated by browser-based AI agents.

It instruments supported browser network APIs and emits normalized Senda-Argus events only when traffic is identified as an LLM request or an MCP JSON-RPC message. It does not collect DOM content, form input, clicks, navigation history, or ordinary application API traffic.

> **Patent Notice**
> Certain concepts and techniques related to Senda-Argus, including AI agent execution trace collection, decision trace reconstruction, and runtime audit event correlation, are patent pending in Japan.
> This notice does not change the terms of the Apache License 2.0 applicable to this repository.

## Scope

Collected:

* OpenAI-compatible LLM requests and responses
* Ollama native and OpenAI-compatible endpoints
* Anthropic Messages API requests and responses
* MCP JSON-RPC lifecycle messages
* MCP `tools/call` request, completion, and failure events
* MCP traffic carried over WebSocket
* Request and response hashes, model, endpoint, status, and latency
* Stable `agent_id` and `purpose_id` correlation identifiers

Not collected:

* DOM snapshots or page text
* `MutationObserver` activity
* click, input, change, or submit events
* History API or navigation events
* arbitrary REST, GraphQL, analytics, or static asset traffic
* non-MCP JSON-RPC protocols

## Hook targets

| Target | Status | Hook approach | Typical events |
|---|---|---|---|
| `window.fetch` | Experimental | Global function wrapper with LLM/MCP classification | `llm.*`, `mcp.*` |
| `XMLHttpRequest` | Experimental | Prototype method wrapper with LLM/MCP classification | `llm.*`, `mcp.*` |
| `WebSocket` | Experimental | Constructor and message wrapper for MCP JSON-RPC | `mcp.websocket.*` |

All network calls continue to the original browser implementation. Non-AI traffic is passed through without generating Senda-Argus events.

## One-line installation

Load the script before the browser agent library:

```html
<script
  src="https://your-cdn.example/senda-argus-browser-hooks.js"
  data-project="browser-agent-app"
  data-environment="production"
  data-endpoint="https://argus.example/v1/events"></script>
```

The script automatically registers `fetch`, XHR, and WebSocket hooks.

For local testing without an event collector:

```html
<script
  src="./dist/senda-argus-browser-hooks.js"
  data-project="page-agent-local-test"
  data-environment="local"
  data-exporter="console"></script>
```

## PageAgent with local Ollama

Senda-Argus must be loaded before PageAgent:

```html
<script
  src="./dist/senda-argus-browser-hooks.js"
  data-project="page-agent-local-test"
  data-exporter="console"></script>

<script
  src="https://cdn.jsdelivr.net/npm/page-agent@1.12.2/dist/iife/page-agent.demo.js?autoInit=false"
  crossorigin="anonymous"></script>

<script>
  const agent = new window.PageAgent({
    model: "qwen3:14b",
    baseURL: "http://127.0.0.1:11434/v1",
    apiKey: "ollama",
    language: "en-US"
  });

  await agent.execute("Click the Search button");
</script>
```

PageAgent converts the current page state into LLM input. Senda-Argus observes the resulting LLM request at the network boundary; it does not separately inspect or store the DOM.

## Programmatic usage

```javascript
import SendaArgus from "@senda/argus-browser-hooks";

SendaArgus.register({
  project: "browser-agent-app",
  environment: "production",
  endpoint: "https://argus.example/v1/events",
  capturePrompt: false,
  captureResponse: false,
  captureArguments: false,
  captureResult: false,
  redact: true,
  includeUrlPatterns: [
    "^http://127\\.0\\.0\\.1:11434/",
    "^https://approved-mcp\\.example/"
  ]
});
```

## Classification behavior

### LLM endpoints

The default classifier recognizes common paths such as:

* `/v1/chat/completions`
* `/v1/responses`
* `/v1/completions`
* `/v1/embeddings`
* Ollama `/api/chat`
* Ollama `/api/generate`
* Ollama `/api/embed`
* Ollama `/api/embeddings`
* Anthropic `/v1/messages`

### MCP messages

MCP events require JSON-RPC 2.0 and a known MCP method such as:

* `initialize`
* `tools/list`
* `tools/call`
* `resources/list`
* `resources/read`
* `prompts/list`
* `prompts/get`
* supported MCP notifications

A generic JSON-RPC request such as an Ethereum RPC method is not treated as MCP.

## Event types

### LLM

* `llm.request`
* `llm.response`
* `llm.error`

### MCP

* `mcp.requested`
* `mcp.completed`
* `mcp.failed`
* `mcp.tool_call.requested`
* `mcp.tool_call.completed`
* `mcp.tool_call.failed`
* `mcp.websocket.sent`
* `mcp.websocket.received`

### Runtime

* `browser.ai.instrumented`

The runtime event records that the active scope is limited to `llm` and `mcp`.

## Capture and redaction controls

Raw content is disabled by default.

| Option | Default | Description |
|---|---:|---|
| `capturePrompt` | `false` | Store raw LLM request payloads |
| `captureResponse` | `false` | Store raw LLM response payloads |
| `captureArguments` | `false` | Store MCP arguments and selected request headers |
| `captureResult` | `false` | Store MCP result payloads |
| `captureHash` | `true` | Emit hashes for correlation when raw content is disabled |
| `redact` | `true` | Redact tokens, credentials, cookies, and known secret fields |

Recommended production posture:

```javascript
SendaArgus.register({
  capturePrompt: false,
  captureResponse: false,
  captureArguments: false,
  captureResult: false,
  captureHash: true,
  redact: true
});
```

## URL filtering

Use an allowlist to reduce interception overhead and prevent accidental classification of unrelated endpoints:

```javascript
SendaArgus.register({
  includeUrlPatterns: [
    "^http://127\\.0\\.0\\.1:11434/",
    "^https://llm-gateway\\.example/",
    "^https://mcp\\.example/"
  ]
});
```

Optional exclusions:

```javascript
SendaArgus.register({
  excludeUrlPatterns: [
    "google-analytics",
    "googletagmanager",
    "sentry"
  ]
});
```

## Development

```bash
npm install
npm run build
npm test
```

The test suite verifies:

* schema 0.2 event generation
* redaction
* OpenAI-compatible LLM request/response capture
* MCP `tools/call` lifecycle capture
* ordinary REST traffic is ignored
* non-MCP JSON-RPC traffic is ignored

## Security and privacy notes

Browser-side hooks may observe sensitive AI request and response data. Keep raw body capture disabled unless it is operationally required. Use HTTPS for event delivery, configure URL allowlists, review collector access controls, and treat exported events as security audit data.

The SDK intentionally avoids collecting page content and general user interaction events. This keeps its scope aligned with AI runtime observability rather than browser session recording.

## Limitations

* Endpoint classification is heuristic and may require allowlist tuning for custom gateways.
* Streaming response bodies are not expanded into per-token or per-chunk events.
* Browser monkey patches can be bypassed by isolated execution contexts, service workers, browser extensions, or clients that retain native API references before registration.
* WebSocket MCP response messages may not contain the original method name; correlation is based on endpoint and JSON-RPC metadata.
* The SDK provides observation only. It does not block, enforce policy, score risk, or provide a dashboard.

## License

Apache License 2.0. See [LICENSE](./LICENSE).
