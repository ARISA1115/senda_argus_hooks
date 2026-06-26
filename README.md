# Senda-Argus Hooks

Senda-Argus Hooks is a hook-only observability SDK for LLM, MCP, and agent runtime audit events.

It collects normalized execution events from SDK hooks, monkey patches, and runtime hooks without requiring application-level `audit.event()` calls or business logic changes in agent applications.

The collected events are designed for downstream analysis, correlation, risk scoring, alerting, and visualization by external systems such as Argus.

## Features

* Hook-only event collection
* No required `audit.event()` calls in application logic
* LLM request and error event collection
* MCP tool call request, completion, and failure event collection
* Agent and PromptOps runtime event examples
* JSONL, stdout, null, and Parquet exporters
* Redaction and capture controls
* Stable correlation identifiers:

  * `agent_id`
  * `purpose_id`
  * `mcp_profile_id`
* CLI tools for validation, inspection, trace viewing, statistics, and conversion
* Unit tests that do not require external API keys

## Hook targets

| Target                         | Status             | Hook approach                  | Test coverage                  | Typical events                                                               |
| ------------------------------ | ------------------ | ------------------------------ | ------------------------------ | ---------------------------------------------------------------------------- |
| OpenAI SDK                     | Experimental       | SDK method hook / monkey patch | Fake SDK hook test             | `llm.request`, `llm.error`                                                   |
| Anthropic SDK                  | Experimental       | SDK method hook / monkey patch | Fake SDK hook test             | `llm.request`, `llm.error`                                                   |
| LiteLLM                        | Experimental       | SDK method hook / monkey patch | Fake SDK hook test             | `llm.request`, `llm.error`                                                   |
| MCP Python SDK                 | Experimental       | Client/session hook            | Fake `ClientSession` hook test | `mcp.tool_call.requested`, `mcp.tool_call.completed`, `mcp.tool_call.failed` |
| Built-in mock MCP client       | Tested             | Runtime hook example           | Unit test                      | `mcp.tool_call.requested`, `mcp.tool_call.completed`, `mcp.tool_call.failed` |
| PromptOps / Agent examples     | Tested as examples | Runtime hook example           | Unit test                      | `agent.decision`, `promptops.run.completed`                                  |
| Built-in Ollama example client | Example            | Runtime hook example           | Example client hook            | `llm.request`, `llm.error`                                                   |

External SDK integrations are marked as experimental because SDK internal class names and method locations may change between releases. The repository includes fake SDK compatibility tests to validate hook behavior without requiring real API keys.

## Tested SDK versions

Senda-Argus Hooks uses SDK method hooks and monkey patches. Compatibility may vary when SDK internals change.

The following versions were installed in a clean virtual environment and used for smoke testing.

| Target | Tested version | Test type | Status |
|---|---:|---|---|
| OpenAI SDK | 2.30.0 | Import/patch smoke test; real SDK error-path hook test with `AuthenticationError` | Experimental |
| Anthropic SDK | 0.112.0 | Import/patch smoke test; real SDK error-path hook test with `AuthenticationError` | Experimental |
| LiteLLM | 1.83.7 | Import/patch smoke test; real SDK error-path hook test with provider authentication failure | Experimental |
| MCP Python SDK | 1.28.0 | Import/patch smoke test; fake `ClientSession` unit test; built-in mock MCP hook smoke test | Experimental |
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

This allows different agent implementations to be grouped together when they use the same MCP endpoint and tool capability.

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
| `capture_arguments` | Capture MCP tool arguments                            |
| `capture_result`    | Capture MCP tool results                              |
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
rm -rf dist build *.egg-info

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
* PromptOps and built-in mock runtime events

Tests do not require external API keys.

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
python -m pip install /path/to/senda_argus_hooks-0.2.0-py3-none-any.whl

senda-hooks --help
```

Then run the smoke tests above.

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

### External SDK hooks do not emit events

External SDK integrations are experimental. SDK internals may change between versions.

Check that:

* the target SDK is installed in the active environment
* `auto_instrument=True` is enabled
* the SDK method being used is one of the hooked methods
* the application imports and registers hooks before creating or using SDK clients

## License

Apache License 2.0. See [LICENSE](./LICENSE).
