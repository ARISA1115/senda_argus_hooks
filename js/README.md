# Senda-Argus Hooks JS v0.2.0

Node.js / TypeScript hook package for Senda-Argus Hooks. JS v0.2.0 is included in project release v0.7.0; the Python package remains independently versioned.

## Scope

- Core: AsyncLocalStorage context, event schema 0.2, SHA-256 hashing, redaction, JSONL exporter
- OpenAI: `responses.create`, `chat.completions.create`, `embeddings.create`
- Anthropic: `messages.create`
- Ollama: `chat`
- MCP: `Client.callTool`

## Build and test

```bash
npm install
npm run build
npm test
```

Production sources compile from `src/` directly into `dist/`, so the package entry points `dist/index.js` and `dist/index.d.ts` exist after `npm run build`. Test sources compile separately into `dist-test/`.

To inspect the publishable package contents without publishing:

```bash
npm run test:package
```

## Usage

```ts
import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";
import ollama from "ollama";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { register, shutdown } from "@senda/argus-hooks";

const openai = new OpenAI();
const anthropic = new Anthropic();
const mcp = new Client({ name: "my-client", version: "1.0.0" });

register({
  project: "example-agent",
  environment: "dev",
  exporters: [{ type: "jsonl", path: "./logs/events.jsonl" }],
  capturePrompt: false,
  captureResponse: false,
  captureArguments: true,
  captureResult: false,
  redact: true
}, {
  openai,
  anthropic,
  ollama,
  mcp,
  mcpMetadata: { serverName: "example-mcp" }
});

// Use SDKs normally. No audit.event() calls are required.

await shutdown();
```

### Why targets are passed to `register()`

Node.js ESM exports cannot be safely replaced globally after import in the same way Python classes can be monkey-patched. The JS package therefore patches the supported SDK client/module objects directly. This remains hook-only with respect to business logic: applications keep calling their normal SDK methods and do not emit audit events themselves.

A preload/loader based ESM auto-instrumentation layer can be added in a later JS release without changing the normalized event schema.

Exporter configuration objects compatible with the Python package are supported for `jsonl`, `stdout`, and `null`, while custom exporter instances remain supported.

## Framework integrations (JS v0.2.0 / Senda-Argus Hooks v0.7.0)

The Node/TypeScript package also provides framework-level integrations. These remain optional and are loaded by the application only when the corresponding framework is used.

### LangChain JS

Pass the callback handler through the normal LangChain callback configuration:

```ts
import { register, langChainCallbackHandler } from "@senda/argus-hooks";

register({ project: "langchain-app" });
const handler = langChainCallbackHandler();

const result = await chain.invoke(input, {
  callbacks: [handler],
});
```

Typical normalized events include `llm.request.*`, `tool_call.*`, `agent.step.*`, and `agent.decision`.

### LangGraph JS

For predictable instrumentation, use the wrappers:

```ts
import { invokeWithArgus, streamWithArgus } from "@senda/argus-hooks";

const result = await invokeWithArgus(graph, input);

for await (const chunk of streamWithArgus(graph, input, { streamMode: "updates" })) {
  console.log(chunk);
}
```

`instrumentLangGraph(graph)` is also provided as a best-effort in-place patch for graph objects whose `invoke` / `stream` properties are writable.

### LlamaIndex TS

Instrument the concrete component instances used by the application:

```ts
import { instrumentLlamaIndex } from "@senda/argus-hooks";

instrumentLlamaIndex({
  retriever,
  embedModel,
  queryEngine,
});
```

This emits normalized `retrieval.*`, `embedding.*`, and `rag.query.*` events.

### Vercel AI SDK

Senda-Argus exposes middleware compatible with AI SDK wrapping patterns (verified with AI SDK v7 `generateText()`):

```ts
import { wrapLanguageModel } from "ai";
import { sendaArgusLanguageModelMiddleware } from "@senda/argus-hooks";

const monitoredModel = wrapLanguageModel({
  model,
  middleware: sendaArgusLanguageModelMiddleware(),
});
```

The middleware wraps `doGenerate` and `doStream` and normalizes them to `llm.request` / `llm.error` events. Streaming response chunks are not individually persisted in this release.

### OpenAI Agents SDK JS/TS

The OpenAI Agents SDK exposes custom trace processors. Register Senda-Argus as an additional processor:

```ts
import * as Agents from "@openai/agents";
import { instrumentOpenAIAgents } from "@senda/argus-hooks";

instrumentOpenAIAgents(Agents);
```

Generation, function/tool, handoff, and agent spans are mapped to normalized Senda-Argus events. The integration uses `addTraceProcessor()` and does not replace the SDK's existing trace processors.

### Test coverage

The JS test suite includes deterministic compatibility tests for:

- OpenAI SDK hooks
- MCP `Client.callTool`
- LangChain callback lifecycle
- LangGraph invoke and stream lifecycle
- LlamaIndex retrieval / embedding / query instrumentation
- Vercel AI SDK middleware
- OpenAI Agents tracing processor integration, including `onTraceStart`, `onTraceEnd`, `onSpanStart`, `onSpanEnd`, `forceFlush`, and `shutdown`
- lifecycle trace correlation for MCP, LangChain, LangGraph, and LlamaIndex

Run:

```bash
cd js
npm install
npm test
npm run test:package
```

For a local packed-package smoke test, run `npm pack`, install the generated `.tgz` into a clean temporary project, and verify that `import "@senda/argus-hooks"` succeeds.
