# Senda-Argus Hooks JS v0.1

Node.js / TypeScript hook package included with Senda-Argus Hooks v0.6.0.

## Scope

- Core: AsyncLocalStorage context, event schema 0.2, SHA-256 hashing, redaction, JSONL exporter
- OpenAI: `responses.create`, `chat.completions.create`, `embeddings.create`
- Anthropic: `messages.create`
- Ollama: `chat`
- MCP: `Client.callTool`

## Build

```bash
npm install
npm test
```

## Usage

```ts
import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";
import ollama from "ollama";
import { Client } from "@modelcontextprotocol/client";
import { register, shutdown } from "@senda/argus-hooks";

const openai = new OpenAI();
const anthropic = new Anthropic();
const mcp = new Client({ name: "my-client", version: "1.0.0" });

register({
  project: "example-agent",
  environment: "dev",
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

Node.js ESM exports cannot be safely replaced globally after import in the same way Python classes can be monkey-patched. v0.1 therefore patches the supported SDK client/module objects directly. This remains hook-only with respect to business logic: applications keep calling their normal SDK methods and do not emit audit events themselves.

A preload/loader based ESM auto-instrumentation layer can be added in a later JS release without changing the normalized event schema.

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

Senda-Argus exposes a Language Model V3 middleware compatible with current AI SDK middleware/wrapping patterns:

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
- Vercel AI SDK Language Model V3 middleware
- OpenAI Agents tracing processor integration

Run:

```bash
cd js
tsc -p tsconfig.json
node --test dist/tests/*.test.js
```
