# Senda-Argus Hooks

Senda-Argus Hooks is a hook-only observability SDK for LLM, MCP, agent runtime, and RAG audit events.

It collects normalized execution events from SDK hooks, monkey patches, and runtime hooks without requiring application-level `audit.event()` calls or business logic changes in agent applications.

The collected events are designed for downstream analysis, correlation, risk scoring, alerting, and visualization by external systems such as Argus.

> **Patent Notice**
> Certain concepts and techniques related to Senda-Argus, including AI agent execution trace collection, decision trace reconstruction, and runtime audit event correlation, are patent pending in Japan.
> This notice does not change the terms of the Apache License 2.0 applicable to this repository.

## Features

* Hook-only event collection
* No required `audit.event()` calls in application logic
* LLM request and error event collection
* Native Ollama Python SDK hook for `ollama.chat(...)` and `ollama.Client.chat(...)`
* MCP tool call request, completion, and failure event collection
* Generic non-MCP tool call request, completion, and failure event collection
* RAG retrieval, embedding, and query lifecycle event collection
* OpenAI Agents SDK, LangChain, LangGraph, and LlamaIndex integration examples
* Langflow example integration for GUI-built agent, RAG, and MCP workflows
* Agent and PromptOps runtime event examples
* JSONL, stdout, null, and Parquet exporters
* Redaction and capture controls
* Stable correlation identifiers:

  * `agent_id`
  * `purpose_id`
  * `mcp_profile_id`
* CLI tools for validation, inspection, trace viewing, statistics, and conversion
* Unit tests that do not require external API keys

## v0.5.0 verification summary

The v0.5.0 release adds native Ollama Python SDK instrumentation and a Langflow example integration. The following local verification was completed on macOS with an Apple Silicon arm64 Python 3.12 environment:

* `pytest` passed with the Ollama SDK instrumentor tests included.
* `ollama.Client.chat(...)` was verified against `argus-qwen25-14b-toolplan:latest`.
* Langflow started successfully and loaded custom components from `LANGFLOW_COMPONENTS_PATH`.
* A Langflow custom component calling `ollama.Client.chat(...)` emitted a privacy-safe `llm.request` event to JSONL.
* `senda-hooks validate` returned `valid: true`.
* `senda-hooks inspect --summary` reported one `llm.request` event for the Langflow/Ollama smoke test.

The verified event included `source.sdk = "ollama"`, `source.operation = "Client.chat"`, `model = "argus-qwen25-14b-toolplan:latest"`, and `security.redacted = true`. Raw prompt and response bodies were not stored by default; hashes and metadata were emitted instead.

## Hook targets

| Target                         | Status             | Hook approach                  | Test coverage                  | Typical events                                                               |
| ------------------------------ | ------------------ | ------------------------------ | ------------------------------ | ---------------------------------------------------------------------------- |
| OpenAI SDK                     | Experimental       | SDK method hook / monkey patch | Fake SDK hook test             | `llm.request`, `llm.error`                                                   |
| Anthropic SDK                  | Experimental       | SDK method hook / monkey patch | Fake SDK hook test             | `llm.request`, `llm.error`                                                   |
| LiteLLM                        | Experimental       | SDK method hook / monkey patch | Fake SDK hook test             | `llm.request`, `llm.error`                                                   |
| Ollama Python SDK              | Experimental       | SDK method hook / monkey patch | Fake SDK hook test             | `llm.request`, `llm.error`                                                   |
| MCP Python SDK                 | Experimental       | Client/session hook            | Fake `ClientSession` hook test | `mcp.tool_call.requested`, `mcp.tool_call.completed`, `mcp.tool_call.failed` |
| OpenAI Agents SDK | Experimental | Runner hook / trace processor helper | Real SDK import/patch smoke test; invalid API key error-path test | `agent.run.*`, `agent.step.*`, `tool_call.*`, `llm.*` |
| LangChain | Experimental | Callback handler | Real `CallbackManager` smoke test | `llm.*`, `tool_call.*`, `agent.step.*`, `agent.decision` |
| LangGraph | Experimental | Stream wrapper / event stream integration | Real `StateGraph` stream smoke test | `agent.run.*`, `agent.step.*` |
| LlamaIndex / RAG | Experimental | `register(..., rag={...})` / `instrument_rag()` / wrapper helpers | Fake component tests; `register(..., rag={...})` smoke test | `retrieval.*`, `embedding.*`, `rag.query.*` |
| Langflow | Example | Custom component helper / LangChain callback reuse / SDK hooks | Compile smoke test; local custom component smoke test | `llm.*`, `tool_call.*`, `mcp.tool_call.*`, `retrieval.*`, `agent.step.*` |
| Built-in mock MCP client | Tested | Runtime hook example | Unit test | `mcp.tool_call.requested`, `mcp.tool_call.completed`, `mcp.tool_call.failed` |
| PromptOps / Agent examples     | Tested as examples | Runtime hook example           | Unit test                      | `agent.decision`, `promptops.run.completed`                                  |
| Built-in Ollama example client | Example            | Runtime hook example           | Example client hook            | `llm.request`, `llm.error`                                                   |

External SDK integrations are marked as experimental because SDK internal class names and method locations may change between releases. The repository includes fake SDK compatibility tests to validate hook behavior without requiring real API keys.

## Integration status

Senda-Argus Hooks separates low-level SDK hooks from framework integrations.

* SDK hooks use method hooks or monkey patches where appropriate.
* Framework integrations use callback handlers, stream wrappers, or best-effort runner hooks.
* External framework packages are optional and are not required by the base installation.

| Integration | Status | Integration type | Introduced | Notes |
|---|---|---|---:|---|
| OpenAI SDK | Experimental | SDK method hook / monkey patch | v0.2.0 | Captures `llm.request` and `llm.error` where supported |
| Anthropic SDK | Experimental | SDK method hook / monkey patch | v0.2.0 | Captures `llm.request` and `llm.error` where supported |
| LiteLLM | Experimental | SDK method hook / monkey patch | v0.2.0 | Wrapper SDKs may also invoke lower-level provider SDKs |
| Ollama Python SDK | Experimental | SDK method hook / monkey patch | v0.5.0 | Captures native `ollama.chat(...)` and `ollama.Client.chat(...)` calls; install optional `ollama` package separately |
| MCP Python SDK | Experimental | Client/session hook | v0.2.0 | Captures MCP `ClientSession.call_tool` lifecycle events |
| OpenAI Agents SDK | Experimental | Runner/tracing integration | v0.3.0 | Captures agent run lifecycle events and tracing-style spans |
| LangChain | Experimental | Callback handler | v0.3.0 | Captures LLM, tool, chain, and agent callback events |
| LangGraph | Experimental | Stream wrapper / event stream integration | v0.3.0 | Captures graph run and step events from streamed execution |
| LlamaIndex / RAG | Experimental | `register(..., rag={...})` / `instrument_rag()` / wrapper helpers | v0.4.0 | Captures retrieval, embedding, and query lifecycle events |
| Langflow | Example | Custom component helper / LangChain callback reuse / SDK hooks | v0.5.0 example | Use `examples/langflow/` to monitor Langflow-based agent, RAG, and MCP workflows |
| Built-in mock MCP client | Tested | Runtime hook example | v0.2.0 | Used for local tests and smoke tests |
| PromptOps examples | Tested as examples | Runtime hook example | v0.2.0 | Used for local tests and smoke tests |
| Built-in Ollama example client | Example | Runtime hook example | v0.2.0 | Useful for local error-path checks |

Real API success-path tests for OpenAI, Anthropic, and LiteLLM require valid provider API keys. Public tests do not require external API keys. Error-path tests with invalid credentials are useful for confirming that SDK hooks emit `llm.error` events.

When multiple SDK hooks are enabled at the same time, wrapper SDKs such as LiteLLM may also call lower-level provider SDKs. In that case, multiple events may be emitted for a single application-level request. Disable lower-level hooks if you only want wrapper-level events.


## Ollama Python SDK hook

Senda-Argus Hooks supports the official Ollama Python SDK as an optional instrumentor. The `ollama` package is not a required dependency of the base installation. Install it only when you need local Ollama SDK tracing:

```bash
pip install ollama
# or
pip install 'senda-argus-hooks[ollama]'
```

Supported in v0.5.0:

* `ollama.chat(...)`
* `ollama.Client.chat(...)`

Example:

```python
import ollama
from senda_argus_hooks import register, flush, shutdown

register(
    project="ollama-local-test",
    environment="local",
    auto_instrument=True,
    exporters=[{"type": "jsonl", "path": "./logs/ollama-events.jsonl"}],
    capture_prompt=False,
    capture_response=False,
    redact=True,
)

try:
    client = ollama.Client(host="http://127.0.0.1:11434")
    client.chat(
        model="argus-qwen25-14b-toolplan:latest",
        messages=[{"role": "user", "content": "Why is tool-call logging important?"}],
    )
finally:
    flush()
    shutdown()
```

Current limitations:

* Streaming calls are recorded as request/error events and successful returned stream objects are not expanded into per-chunk events.
* `ollama.AsyncClient`, `generate()`, and `embeddings()` are planned for a future release.

Local verification command:

```bash
mkdir -p logs
export OLLAMA_BASE_URL="http://127.0.0.1:11434"
export OLLAMA_MODEL="argus-qwen25-14b-toolplan:latest"
export ARGUS_EXPORT_PATH="./logs/langflow-events.jsonl"

python examples/ollama_sdk_basic.py
senda-hooks validate ./logs/langflow-events.jsonl
senda-hooks inspect ./logs/langflow-events.jsonl --summary
```

Expected summary:

```json
{
  "count": 1,
  "event_types": {
    "llm.request": 1
  }
}
```

## Langflow example integration

Langflow support is provided as an example integration under `examples/langflow/`.

Langflow workflows may combine LLM providers, LangChain-compatible components, LangGraph-style agent flows, RAG retrievers, MCP tools, and custom Python components. Senda-Argus Hooks can be registered near the beginning of a Langflow flow to collect normalized audit events from these execution paths where hooks and callbacks are available.

Recommended approaches:

* Add `examples/langflow/custom_component_senda_argus_register.py` as a Langflow custom component.
* Use `senda_argus_hooks.integrations.langflow.register_langflow_hooks()` from a Python/custom component.
* Use `senda_argus_hooks.integrations.langflow.langflow_callback_handler()` for LangChain-compatible Langflow components that accept callbacks.
* Enable MCP hooks when the Langflow flow connects to external MCP servers.
* For guaranteed Ollama SDK-level logging, call `ollama.chat(...)` or `ollama.Client.chat(...)` from a Langflow custom component after registering hooks.

Default privacy posture for the example is prompt/response capture disabled, tool argument capture enabled, result-body capture disabled, and redaction enabled.

### Verified Langflow + Ollama SDK path

The v0.5.0 local smoke test verified this path:

```text
Langflow custom component
  -> senda_argus_hooks.register(auto_instrument=True)
  -> ollama.Client.chat(model="argus-qwen25-14b-toolplan:latest")
  -> JSONL exporter
  -> senda-hooks validate / inspect
```

Observed event characteristics:

* `event_type`: `llm.request`
* `source.sdk`: `ollama`
* `source.operation`: `Client.chat`
* `model`: `argus-qwen25-14b-toolplan:latest`
* `security.redacted`: `true`
* raw prompt and response bodies disabled by default
* prompt, message, argument, and response hashes emitted for correlation

### Important Langflow note

Langflow built-in nodes may use internal HTTP clients or framework-specific wrappers instead of the provider SDK directly. SDK-level hooks only observe calls that pass through supported SDK surfaces such as `ollama.Client.chat(...)`, OpenAI SDK methods, Anthropic SDK methods, LiteLLM methods, MCP client methods, or supported callback integrations.

If a built-in Langflow Ollama node does not emit events, use one of these approaches:

* call the Ollama SDK from a Langflow custom component
* pass the Senda-Argus LangChain callback handler to LangChain-compatible components
* add a future HTTP/client-level instrumentor for the specific built-in component implementation

## Agentic AI security monitoring

AI workflow platforms can become high-value attack surfaces because they often hold LLM provider keys, cloud credentials, database credentials, MCP connection details, internal tool access, and RAG data source access.

Senda-Argus Hooks is intended to help monitor not only prompt and response activity, but also tool execution, MCP calls, RAG retrieval, retries, and workflow behavior across agentic AI applications. This helps investigate suspicious patterns such as unexpected tool usage, calls to unapproved MCP servers, unusual RAG source access, repeated failure-and-retry behavior, and potentially destructive database or system operations.

See `docs/use-cases/agentic-ransomware-monitoring.md` for the recommended detection and monitoring model.

## Tested SDK versions

Senda-Argus Hooks uses SDK method hooks and monkey patches. Compatibility may vary when SDK internals change.

The following versions were installed in a clean virtual environment and used for smoke testing.

| Target | Tested version | Test type | Status |
|---|---:|---|---|
| OpenAI SDK | 2.30.0 | Import/patch smoke test; real SDK error-path hook test with `AuthenticationError` | Experimental |
| Anthropic SDK | 0.112.0 | Import/patch smoke test; real SDK error-path hook test with `AuthenticationError` | Experimental |
| LiteLLM | 1.83.7 | Import/patch smoke test; real SDK error-path hook test with provider authentication failure | Experimental |
| MCP Python SDK | 1.28.0 | Import/patch smoke test; fake `ClientSession` unit test; built-in mock MCP hook smoke test | Experimental |
| OpenAI Agents SDK | Installed in real SDK smoke test | Import/patch smoke test; invalid API key error-path test; verified `agent.run.started`, `agent.run.failed` | Experimental |
| LangChain | Installed in real SDK smoke test | Real `CallbackManager` smoke test; verified `llm.request.started`, `llm.request`, `tool_call.requested`, `tool_call.completed` | Experimental |
| LangGraph | Installed in real SDK smoke test | Real `StateGraph.stream` wrapper smoke test; verified `agent.run.started`, `agent.step.completed`, `agent.run.completed` | Experimental |
| LlamaIndex / RAG | Optional / wrapper based | `register(..., rag={...})` smoke test; verified `retrieval.requested`, `retrieval.completed`, `embedding.requested`, `embedding.completed`, `rag.query.started`, `rag.query.completed`; `senda-hooks retrievals` verified | Experimental |
| Ollama Python SDK | 0.6.2 local smoke test / optional | Fake SDK hook test for `ollama.chat` and `Client.chat`; local `argus-qwen25-14b-toolplan:latest` smoke test verified `llm.request` | Experimental |
| Langflow | Local macOS arm64 Python 3.12 smoke test | Custom component using `ollama.Client.chat(...)`; verified JSONL `llm.request` and CLI validation | Example |
| Built-in mock MCP client | packaged | Unit test and hook smoke test | Tested |
| PromptOps examples | packaged | Unit test and hook smoke test | Tested |
| Built-in Ollama example client | packaged | Error-path hook smoke test | Example |

## What is collected

### Common event fields

Each event is normalized with common fields such as:

* `schema_version`
* `event_id`
* `trace_id`
* `span_id`
* `parent_span_id`
* `timestamp`
* `project`
* `environment`
* `event_type`
* `source`
* `actor`
* `data`
* `security`
* `status`
* `latency_ms`
* `error`

These fields are intended to make LLM calls, MCP tool calls, and agent runtime events reconstructable as a trace.

### LLM events

Typical LLM events include:

* `llm.request`
* `llm.error`

Depending on the SDK and capture settings, LLM event data may include:

* provider
* operation
* model
* input hash
* output hash
* prompt or message payload, if enabled
* response payload, if enabled
* latency
* error type and message

Prompt and response capture is configurable and can be disabled for privacy and security.

### MCP tool call events

MCP tool calls are split into lifecycle events:

* `mcp.tool_call.requested`
* `mcp.tool_call.completed`
* `mcp.tool_call.failed`

MCP event data may include:

* MCP server name
* normalized MCP server URL
* tool name
* capability
* arguments hash
* result hash
* arguments, if enabled
* result, if enabled
* `purpose_id`
* `mcp_profile_id`
* latency
* error details

### Generic tool call events

Non-MCP tools from agent frameworks such as OpenAI Agents SDK, LangChain, and LangGraph may emit generic tool call lifecycle events:

* `tool_call.requested`
* `tool_call.completed`
* `tool_call.failed`

Generic tool event data may include:

* framework name
* tool name
* tool type
* operation
* target
* arguments hash
* result hash
* `purpose_id`
* latency
* error details

Generic `tool_call.*` events are intended for tools that are not necessarily MCP tools, such as Python functions, HTTP APIs, shell commands, file operations, retrievers, browser actions, cloud APIs, and collaboration tools.

### RAG retrieval and embedding events

v0.4.0 adds RAG-oriented audit events for knowledge access. These events are intended to show what knowledge source was searched and what embedding/retrieval activity happened before an LLM response was generated.

Typical RAG events include:

* `retrieval.requested`
* `retrieval.completed`
* `retrieval.failed`
* `embedding.requested`
* `embedding.completed`
* `embedding.failed`
* `rag.query.started`
* `rag.query.completed`
* `rag.query.failed`

Retrieval event data may include:

* framework name
* retriever name
* retriever type
* query hash
* `top_k`
* index name
* collection name
* vector store
* result count
* document ID hash
* chunk ID hash
* score min/max
* `purpose_id`
* latency
* error details

Embedding event data may include:

* framework name
* provider
* model
* input hash
* input count
* input length
* vector dimension
* vector count
* vector hash
* `purpose_id`

Raw retrieval queries and embedding inputs follow the same capture/redaction controls as tool arguments. Raw retrieved results follow `capture_result`. Hashes are emitted by default for correlation without storing full content.

### Agent and framework runtime events

Agent frameworks and runtime examples may emit:

* `agent.run.started`
* `agent.run.completed`
* `agent.run.failed`
* `agent.step.started`
* `agent.step.completed`
* `agent.step.failed`
* `agent.decision`
* `agent.handoff.started`
* `agent.handoff.completed`
* `promptops.run.completed`
* `promptops.error`

These events are useful for reconstructing agent execution flow, graph steps, callback activity, tool usage, and final run completion.

### Agent and PromptOps events

Agent and PromptOps examples may emit:

* `agent.decision`
* `promptops.run.completed`
* `promptops.error`

These events are useful for correlating tool usage with agent-level decisions and run completion.

## Identity model

Senda-Argus Hooks separates execution identity from capability and purpose identity.

### `agent_id`

`agent_id` identifies the execution origin.

It is generated from runtime metadata such as:

* project
* environment
* SDK or runtime source
* optional agent hint

This is intended to answer:

> Which agent or runtime produced this event?

### `purpose_id`

`purpose_id` identifies a capability or purpose grouping.

It is derived from MCP-related metadata such as:

* MCP server name
* normalized MCP server URL
* tool name
* capability
* optional tool schema hash
* optional tool description hash

For non-MCP tools, `purpose_id` can also be derived from framework/tool metadata such as:

* framework name
* tool name
* tool type
* operation
* target
* optional input schema hash
* optional tool description hash

For RAG retrieval and embedding events, `purpose_id` can be derived from stable knowledge-access metadata such as:

* framework name
* retriever name
* retriever type
* index name
* collection name
* vector store
* embedding provider
* embedding model

This allows different agent implementations to be grouped together when they use the same MCP endpoint, tool capability, framework-level tool profile, retriever/index, or embedding model.

This is intended to answer:

> Which external capability or purpose does this event represent?

### `mcp_profile_id`

`mcp_profile_id` identifies an MCP server or tool profile.

It is derived from MCP server metadata such as:

* server name
* normalized server URL
* optional tool profile information

This is intended to answer:

> Which MCP server profile was used?

## Installation

### Local development install

```bash
git clone <repository-url>
cd senda_argus_hooks

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e .
```

### Install with development dependencies

```bash
python -m pip install -e ".[dev]"
```

### Install with Parquet support

```bash
python -m pip install -e ".[parquet]"
```

### Install with development and Parquet support

```bash
python -m pip install -e ".[dev,parquet]"
```

### Optional framework SDKs

External framework SDKs are optional. Install only the packages you use in your application.

```bash
python -m pip install openai-agents langchain langgraph llama-index-core
```

Package names and versions may vary by project and release. Senda-Argus Hooks does not require these packages for the base installation or unit tests.

## Basic usage

Register hooks near the application entry point.

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

# Run your normal LLM / Agent / MCP application here.
# No application-level audit.event() call is required.

shutdown()
```

For long-running applications, call `shutdown()` during graceful termination.

## Hook-only design

Senda-Argus Hooks does not require agent applications to call `audit.event()` directly.

Application code should continue to call LLM SDKs, MCP clients, or agent frameworks normally. Hooks collect runtime events from supported SDK or runtime surfaces.

Recommended usage:

```python
from senda_argus_hooks import register

register(auto_instrument=True)
```

Avoid adding audit-specific calls to agent business logic unless you explicitly want custom application events.

## Framework integrations

### OpenAI Agents SDK

OpenAI Agents SDK integration is experimental.

When `auto_instrument=True` is enabled, Senda-Argus Hooks attempts a best-effort patch of supported OpenAI Agents SDK runner methods when the SDK is installed.

```python
from senda_argus_hooks import register, shutdown

register(
    project="openai-agents-app",
    environment="dev",
    auto_instrument=True,
    exporters=[{"type": "jsonl", "path": "./logs/agents.jsonl"}],
)

# Run your normal OpenAI Agents SDK application here.

shutdown()
```

For tracing-style integration, the package also provides:

```python
from senda_argus_hooks.integrations.openai_agents import SendaArgusOpenAIAgentsProcessor
```

The processor is designed to convert agent run, tool, handoff, and LLM spans into normalized Senda-Argus events where supported by the installed OpenAI Agents SDK version.

### LangChain Callback Handler

LangChain integration is experimental and uses a callback handler.

```python
from senda_argus_hooks import register, shutdown
from senda_argus_hooks.integrations import SendaArgusCallbackHandler

register(
    project="langchain-app",
    environment="dev",
    exporters=[{"type": "jsonl", "path": "./logs/langchain.jsonl"}],
)

handler = SendaArgusCallbackHandler()

# Pass handler to LangChain callbacks where supported by your chain, tool, model, or agent.
# Example:
# result = chain.invoke(input_data, config={"callbacks": [handler]})

shutdown()
```

Typical events include:

* `llm.request.started`
* `llm.request`
* `llm.error`
* `tool_call.requested`
* `tool_call.completed`
* `tool_call.failed`
* `agent.step.started`
* `agent.step.completed`
* `agent.decision`
* `agent.run.completed`

### LangGraph Stream Wrapper

LangGraph integration is experimental and uses stream wrappers.

```python
from senda_argus_hooks import register, shutdown
from senda_argus_hooks.integrations import stream_with_argus

register(
    project="langgraph-app",
    environment="dev",
    exporters=[{"type": "jsonl", "path": "./logs/langgraph.jsonl"}],
)

# for chunk in stream_with_argus(graph, input_data, stream_mode="updates"):
#     print(chunk)

shutdown()
```

Async usage:

```python
from senda_argus_hooks.integrations import astream_with_argus

# async for chunk in astream_with_argus(graph, input_data, stream_mode="updates"):
#     print(chunk)
```

Typical events include:

* `agent.run.started`
* `agent.step.completed`
* `agent.run.completed`
* `agent.run.failed`

### LlamaIndex / RAG instrumentation

LlamaIndex / RAG integration is experimental. The recommended v0.4.0 usage is to enable RAG instrumentation once from `register()`.

This instruments only the component instances you pass in. It does not globally monkey patch LlamaIndex or other RAG frameworks.

```python
from senda_argus_hooks import register, shutdown

register(
    project="rag-app",
    environment="dev",
    exporters=[{"type": "jsonl", "path": "./logs/rag.jsonl"}],
    capture_arguments=False,
    capture_result=False,
    redact=True,
    rag={
        "framework": "llamaindex",
        "retriever": retriever,
        "embed_model": embed_model,
        "query_engine": query_engine,
        "retriever_type": "vector",
        "index_name": "security_knowledge_base",
        "vector_store": "faiss",
        "top_k": 5,
        "provider": "local",
    },
)

# Use your existing RAG code normally.
# The passed component instances are instrumented.
result = retriever.retrieve("CVE-2024-3094")
vector = embed_model.get_text_embedding("CVE-2024-3094")
answer = query_engine.query("CVE-2024-3094")

shutdown()
```

If you prefer to keep registration and RAG instrumentation separate, use `instrument_rag()`.

```python
from senda_argus_hooks import register, shutdown
from senda_argus_hooks.integrations import instrument_rag

register(
    project="rag-app",
    environment="dev",
    exporters=[{"type": "jsonl", "path": "./logs/rag.jsonl"}],
    capture_arguments=False,
    capture_result=False,
    redact=True,
)

instrument_rag(
    framework="llamaindex",
    retriever=retriever,
    embed_model=embed_model,
    query_engine=query_engine,
    retriever_type="vector",
    index_name="security_knowledge_base",
    vector_store="faiss",
    top_k=5,
    provider="local",
)

answer = query_engine.query("CVE-2024-3094")

shutdown()
```

Lower-level wrapper helpers are also available when you want to instrument a single operation explicitly.

```python
from senda_argus_hooks.integrations import (
    embed_text_with_argus,
    query_with_argus,
    retrieve_with_argus,
)

result = retrieve_with_argus(
    retriever,
    "CVE-2024-3094",
    retriever_type="vector",
    index_name="security_knowledge_base",
    vector_store="faiss",
    top_k=5,
)
vector = embed_text_with_argus(embed_model, "CVE-2024-3094")
answer = query_with_argus(query_engine, "CVE-2024-3094")
```

Typical events include:

* `retrieval.requested`
* `retrieval.completed`
* `retrieval.failed`
* `embedding.requested`
* `embedding.completed`
* `embedding.failed`
* `rag.query.started`
* `rag.query.completed`
* `rag.query.failed`

For callback-style usage, the package also provides:

```python
from senda_argus_hooks.integrations import SendaArgusLlamaIndexCallbackHandler
```

## Exporters

### JSONL exporter

```python
register(
    project="example-agent",
    exporters=[{"type": "jsonl", "path": "./logs/events.jsonl"}],
)
```

### stdout exporter

```python
register(
    project="example-agent",
    exporters=[{"type": "stdout"}],
)
```

### null exporter

```python
register(
    project="example-agent",
    exporters=[{"type": "null"}],
)
```

### Parquet exporter

```python
register(
    project="example-agent",
    exporters=[{"type": "parquet", "dir": "./logs/parquet"}],
)
```

Parquet support requires the `parquet` extra.

```bash
python -m pip install -e ".[parquet]"
```

## Capture and redaction controls

Senda-Argus Hooks can capture or suppress sensitive data.

```python
register(
    project="example-agent",
    capture_prompt=False,
    capture_response=False,
    capture_arguments=True,
    capture_result=False,
    redact=True,
)
```

Common controls:

| Option              | Description                                           |
| ------------------- | ----------------------------------------------------- |
| `capture_prompt`    | Capture LLM prompt or message payloads when supported |
| `capture_response`  | Capture LLM response payloads when supported          |
| `capture_arguments` | Capture MCP, generic tool, retrieval query, and embedding input payloads |
| `capture_result`    | Capture MCP, generic tool, retrieval result, and RAG query results |
| `redact`            | Apply redaction to configured sensitive values        |

When body capture is disabled, hashes are still useful for correlation without storing raw content.

## CLI

The package installs the `senda-hooks` CLI.

```bash
senda-hooks --help
```

### Validate events

```bash
senda-hooks validate ./logs/events.jsonl
```

### Inspect summary

```bash
senda-hooks inspect ./logs/events.jsonl --summary
```

### Show trace

```bash
senda-hooks trace ./logs/events.jsonl --trace-id trace_xxx
```

### Summarize MCP tool usage

```bash
senda-hooks tools ./logs/events.jsonl
```

### Show event statistics

```bash
senda-hooks stats ./logs/events.jsonl
```

### Summarize RAG retrieval and embedding usage

```bash
senda-hooks retrievals ./logs/events.jsonl
```

### Convert JSONL to Parquet

```bash
senda-hooks convert ./logs/events.jsonl --to parquet --out ./logs/parquet
```

## Smoke test

The following test generates one custom event and validates it.

```bash
mkdir -p logs/argus

python - <<'PY'
from senda_argus_hooks import register, shutdown
from senda_argus_hooks.audit import event

register(
    project="release-test",
    environment="dev",
    exporters=[{"type": "jsonl", "path": "logs/argus/events.jsonl"}],
)

event("custom.event", data={"message": "hello"})

shutdown()
PY

senda-hooks validate logs/argus/events.jsonl
senda-hooks inspect logs/argus/events.jsonl --summary
```

Expected output:

```json
{
  "valid": true
}
```

## Hook smoke test

The following test uses built-in mock MCP and PromptOps clients. It does not require external API keys.

```bash
mkdir -p logs/argus
rm -f logs/argus/hook_events.jsonl

python - <<'PY'
from senda_argus_hooks import register, shutdown
from senda_argus_hooks.sdk import MockMCPClient, PromptOpsClient

register(
    project="release-hook-test",
    environment="dev",
    auto_instrument=True,
    exporters=[{"type": "jsonl", "path": "logs/argus/hook_events.jsonl"}],
    capture_arguments=True,
    capture_result=True,
    redact=True,
)

mcp = MockMCPClient(
    {"lookup": lambda query: {"ok": True, "query": query}},
    server="mock_mcp",
)

promptops = PromptOpsClient()

promptops.agent_decision(selected_tool="lookup")
mcp.call_tool("lookup", {"query": "CVE-2024-3094"}, capability="vulnerability_intelligence")
promptops.run_completed(status="success")

shutdown()
PY

senda-hooks validate logs/argus/hook_events.jsonl
senda-hooks inspect logs/argus/hook_events.jsonl --summary
senda-hooks stats logs/argus/hook_events.jsonl
senda-hooks tools logs/argus/hook_events.jsonl
```

Expected event types:

```text
agent.decision
mcp.tool_call.requested
mcp.tool_call.completed
promptops.run.completed
```

## Real SDK smoke test status

The following smoke tests were verified in a clean virtual environment.

| Target | Result | Verified events |
|---|---|---|
| OpenAI Agents SDK | Passed with invalid API key error-path test | `agent.run.started`, `agent.run.failed` |
| LangChain | Passed with real `CallbackManager` | `llm.request.started`, `llm.request`, `tool_call.requested`, `tool_call.completed` |
| LangGraph | Passed with real `StateGraph.stream` wrapper | `agent.run.started`, `agent.step.completed`, `agent.run.completed` |
| RAG instrumentation | Passed with `register(..., rag={...})` component instrumentation | `retrieval.requested`, `retrieval.completed`, `embedding.requested`, `embedding.completed`, `rag.query.started`, `rag.query.completed` |
| RAG CLI summary | Passed with `senda-hooks retrievals` | Retriever and embedding summary output |
| Ollama Python SDK | Passed with local Ollama model `argus-qwen25-14b-toolplan:latest` | `llm.request`, `source.sdk=ollama`, `source.operation=Client.chat` |
| Langflow custom component | Passed on macOS arm64 Python 3.12 with Langflow custom component calling Ollama SDK | JSONL `llm.request`; `senda-hooks validate` returned `valid: true` |


## Development

### Run lint

```bash
ruff check .
```

### Run tests

```bash
pytest -q -rs
```

### Build package

```bash
python -m pip install build twine
rm -rf dist build
find . -maxdepth 1 -name "*.egg-info" -exec rm -rf {} +

python -m build
python -m twine check dist/*
```

## Test coverage

The test suite covers:

* event schema validation
* JSONL exporter
* Parquet exporter
* redaction
* CLI commands
* identity generation

  * `agent_id`
  * `purpose_id`
  * `mcp_profile_id`
* optional SDK behavior when SDKs are not installed
* fake OpenAI SDK hook behavior
* fake Anthropic SDK hook behavior
* fake LiteLLM hook behavior
* fake MCP Python SDK hook behavior
* fake OpenAI Agents SDK integration behavior
* fake LangChain callback handler behavior
* fake LangGraph stream wrapper behavior
* fake LlamaIndex retrieval, embedding, query, and callback-style helper behavior
* generic `tool_call.*` events for non-MCP tools
* generic `retrieval.*`, `embedding.*`, and `rag.query.*` events for RAG flows
* PromptOps and built-in mock runtime events
* fake Ollama Python SDK hook behavior

Tests do not require external API keys.

The v0.5.0 test suite is expected to pass with:

```text
31 passed, 2 skipped
```

## Release verification

Before publishing a release, run:

```bash
ruff check .
pytest -q -rs
python -m build
python -m twine check dist/*
```

A clean virtual environment smoke test is also recommended:

```bash
cd /tmp
rm -rf senda_argus_hooks_release_test
mkdir senda_argus_hooks_release_test
cd senda_argus_hooks_release_test

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install /path/to/senda_argus_hooks-0.5.0-py3-none-any.whl

senda-hooks --help
```

Then run the smoke tests above.

### macOS Langflow + Ollama verification

For Langflow verification on Apple Silicon macOS, use an arm64 Python environment. Mixing Intel/Rosetta Homebrew under `/usr/local` with arm64 Command Line Tools can cause native dependency build failures for packages such as `bottleneck` or `fastparquet`.

Recommended environment checks:

```bash
uname -m
which brew
brew --prefix
python -c "import platform, sys; print(platform.machine()); print(sys.executable)"
clang --version
```

Expected values on Apple Silicon:

```text
arm64
/opt/homebrew/bin/brew
/opt/homebrew
arm64
Apple clang ... Target: arm64-apple-darwin...
```

Install and run Langflow verification:

```bash
cd /tmp
rm -rf senda-argus-langflow-test
mkdir -p senda-argus-langflow-test
cd senda-argus-langflow-test

/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e /path/to/senda_argus_hooks
python -m pip install ollama langflow

mkdir -p components/argus logs
cp /path/to/senda_argus_hooks/examples/langflow/custom_component_senda_argus_register.py \
  components/argus/senda_argus_register.py

export PYTHONPATH="/path/to/senda_argus_hooks/src:$PYTHONPATH"
export LANGFLOW_COMPONENTS_PATH="$PWD/components"
export ARGUS_EXPORT_PATH="$PWD/logs/langflow-events.jsonl"

langflow run --host 127.0.0.1 --port 7860
```

Then create or load a Langflow custom component that calls `ollama.Client.chat(...)` after registering hooks. Validate the result:

```bash
senda-hooks validate ./logs/langflow-events.jsonl
senda-hooks inspect ./logs/langflow-events.jsonl --summary
```

Expected summary for the minimal Ollama SDK smoke test:

```json
{
  "count": 1,
  "event_types": {
    "llm.request": 1
  }
}
```

## Security and privacy notes

Senda-Argus Hooks may observe sensitive runtime data such as prompts, tool arguments, tool results, and model responses.

For production use:

* disable raw prompt capture unless required
* disable raw response capture unless required
* disable raw tool result capture unless required
* enable redaction
* review generated logs before sharing
* avoid committing runtime logs to public repositories
* treat exported events as security-relevant audit data

## What this project does not do

Senda-Argus Hooks does not provide:

* risk scoring
* alerting
* policy enforcement
* blocking or prevention
* dashboard functionality
* SIEM integration
* Argus API upload/export by default

These functions are expected to be implemented by downstream analysis systems.

## Troubleshooting

### `ModuleNotFoundError: rich`

Install the dependency in your current virtual environment.

```bash
python -m pip install rich
```

If this project is used from another application, ensure that the active Python environment is the one used to run that application.

```bash
which python
which pip
python -m pip show rich
```

### No event file is generated

Check that:

* `register()` was called before the SDK or runtime call
* the exporter path is writable
* `shutdown()` was called for short-lived scripts
* the application actually invoked a hooked SDK or runtime method

### `senda-hooks validate` says the file does not exist

Create or locate the event file first.

```bash
ls -l logs/argus/events.jsonl
```

For a quick test, run the smoke test in this README.

### RAG events do not emit retrieval or embedding events

Check that:

* `register(..., rag={...})` or `instrument_rag(...)` was called before using the RAG component instances
* the same retriever, embedding model, or query engine instances passed to instrumentation are the ones used by the application
* the object exposes the expected method, such as `retrieve`, `aretrieve`, `get_text_embedding`, `get_text_embeddings`, `query`, or `aquery`
* `shutdown()` is called for short-lived scripts
* the exporter path is writable

### External SDK hooks do not emit events

External SDK integrations are experimental. SDK internals may change between versions.

Check that:

* the target SDK is installed in the active environment
* `auto_instrument=True` is enabled
* the SDK method being used is one of the hooked methods
* the application imports and registers hooks before creating or using SDK clients

### Langflow event file is empty

If `logs/langflow-events.jsonl` exists but has zero events, check that:

* the Senda-Argus registration component or helper actually ran in the executed flow
* the exporter path is absolute or points to the expected Langflow working directory
* the flow invoked a supported SDK method after registration
* the built-in Langflow node is not bypassing SDK-level hooks by using an internal HTTP client

For Ollama verification, the confirmed path is a Langflow custom component that calls `ollama.Client.chat(...)`.

### Langflow built-in Ollama node does not emit events

The built-in Langflow Ollama node may use an internal HTTP client or framework wrapper rather than the official Ollama Python SDK. In that case, the Ollama SDK instrumentor will not see the request.

Use one of these alternatives:

* call `ollama.chat(...)` or `ollama.Client.chat(...)` from a Langflow custom component
* use OpenAI-compatible Ollama endpoints through a supported OpenAI SDK hook where appropriate
* add a dedicated instrumentor for the specific internal client used by that Langflow component

### macOS native dependency build errors

If `pip install langflow` fails while building packages such as `bottleneck` or `fastparquet`, check for architecture mismatches. On Apple Silicon, avoid mixing `/usr/local` Intel/Rosetta Homebrew with arm64 Command Line Tools.

Useful checks:

```bash
uname -m
which brew
brew --prefix
python -c "import platform, sys; print(platform.machine()); print(sys.executable)"
clang --version
xcode-select -p
```

Expected Apple Silicon values include `/opt/homebrew`, `platform.machine() == "arm64"`, and `clang` targeting `arm64-apple-darwin`.

If Command Line Tools are broken, reinstall them:

```bash
sudo rm -rf /Library/Developer/CommandLineTools
softwareupdate --list
sudo softwareupdate --install "Command Line Tools for Xcode <version from softwareupdate>"
sudo xcode-select --switch /Library/Developer/CommandLineTools
clang --version
```

## License

Apache License 2.0. See [LICENSE](./LICENSE).
