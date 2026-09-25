# Langflow example integration

This example shows how to use Senda-Argus Hooks with Langflow-based AI agent, RAG, and MCP workflows.

Langflow is often used as a GUI workflow layer above LLM providers, LangChain-compatible components, RAG retrievers, custom Python components, and MCP tools. That makes it a useful place to demonstrate Senda-Argus monitoring because a single flow can combine prompt execution, retrieval, tool calls, and external system access.

## What this example adds

- A lightweight helper: `senda_argus_hooks.integrations.langflow.register_langflow_hooks()`
- A LangChain-compatible callback helper labelled as `langflow`
- A Langflow custom component example that registers Senda-Argus near the beginning of a flow
- A sample event file showing the type of JSONL audit output to expect

## Recommended usage

### 1. Register hooks from a Langflow Python/custom component

Use `custom_component_senda_argus_register.py` as a Langflow custom component. Place it near the beginning of the flow so downstream LLM, tool, MCP, and RAG operations can be correlated.

Default privacy posture:

- Prompt capture: disabled
- Response capture: disabled
- Tool argument capture: enabled
- Tool/MCP/RAG result body capture: disabled
- Redaction: enabled

### 2. Use the callback handler for LangChain-compatible components

For Langflow components that accept LangChain callbacks, use:

```python
from senda_argus_hooks.integrations.langflow import langflow_callback_handler

callbacks = [langflow_callback_handler()]
```

This reuses the existing LangChain integration while marking emitted events with `framework=langflow`.

### 3. Keep Langflow exposure locked down

AI workflow platforms can hold LLM provider keys, cloud credentials, database credentials, MCP connection details, and internal tool access. Treat internet-exposed Langflow instances as high-value attack surfaces.

Senda-Argus Hooks is intended to help answer questions such as:

- Which flow executed?
- Which LLM provider/model was called?
- Which tool or MCP server was invoked?
- Which RAG source was queried?
- Were there repeated retries or failures?
- Did a workflow perform unusual or destructive operations?

## Files

- `custom_component_senda_argus_register.py` - Langflow custom component example.
- `register_in_langflow_python_component.py` - minimal Python component style example.
- `sample_events.jsonl` - sample normalized events.
