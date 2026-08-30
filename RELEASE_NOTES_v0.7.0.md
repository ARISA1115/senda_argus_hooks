# Senda-Argus Hooks v0.7.0

## Summary

v0.7.0 expands the Node.js / TypeScript package from provider SDK hooks into agent-framework and AI-runtime integrations while preserving the shared Senda-Argus event schema (`schema_version = 0.2`).

The JavaScript package version is now `0.2.0`. The Python package version is `0.7.0` so the repository release and Python package release remain aligned.

## Added

### LangChain JS

- `SendaArgusLangChainCallbackHandler`
- `langChainCallbackHandler()`
- LLM lifecycle events
- tool lifecycle events
- chain / agent step lifecycle events
- agent decision events

### LangGraph JS

- `invokeWithArgus()`
- `streamWithArgus()`
- `instrumentLangGraph()` best-effort graph patching
- `agent.run.*` and `agent.step.*` normalization

### LlamaIndex TS

- `instrumentLlamaIndex()`
- retriever instrumentation
- embedding model instrumentation
- query engine instrumentation
- normalized `retrieval.*`, `embedding.*`, and `rag.query.*` events

### Vercel AI SDK

- `sendaArgusLanguageModelMiddleware()`
- Language Model V3 middleware contract
- `wrapGenerate` instrumentation
- `wrapStream` instrumentation
- normalized `llm.request` and `llm.error` events

### OpenAI Agents SDK JS/TS

- `SendaArgusOpenAIAgentsProcessor`
- `instrumentOpenAIAgents()`
- integration through `addTraceProcessor()`
- normalization of generation, tool/function, handoff, and agent spans

## Verification

Local deterministic verification on Node.js v22.16.0 / TypeScript 5.8.3:

- TypeScript compilation passed
- JavaScript tests: 8 passed, 0 failed
- Python regression tests: 61 passed, 2 skipped

The framework tests use compatibility/fake objects and do not require provider API keys or external model calls.

## Current limitations

- LangGraph in-place patching is best-effort; wrapper helpers are preferred when graph methods are not writable.
- Vercel AI SDK streaming is recorded at stream creation/completion boundary; individual stream chunks are not persisted.
- External framework SDK internals and callback contracts may change; framework integrations remain experimental.
- Real provider success-path tests still require the corresponding SDK packages and credentials where applicable.
