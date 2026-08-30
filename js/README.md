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
