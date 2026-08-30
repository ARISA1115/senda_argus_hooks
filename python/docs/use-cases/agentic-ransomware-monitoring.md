# Agentic ransomware and AI workflow monitoring

AI workflow platforms such as Langflow, internal agent builders, RAG applications, and MCP-connected automation layers can become high-value attack surfaces.

They often hold or can reach:

- LLM provider API keys
- Cloud credentials
- Database credentials
- MCP server connection details
- Internal HTTP/API tools
- RAG data sources
- File, shell, browser, or collaboration tools

When these platforms are exposed, misconfigured, or exploited, attackers may use agentic behavior to automate discovery, credential access, lateral movement, database targeting, extortion workflows, or destructive operations.

Senda-Argus Hooks is designed to help monitor this class of risk by collecting normalized, privacy-aware audit events across the AI execution path.

## Monitoring goals

The goal is not only to log prompts and responses. In many security investigations, the more important evidence is workflow behavior:

- Which workflow or agent executed?
- Which LLM model was used?
- Which tools were invoked?
- Which MCP server and tool name were requested?
- Which RAG index or collection was queried?
- Were there repeated retries or failures?
- Did the workflow call a database, shell, browser, file, or cloud tool unexpectedly?
- Were potentially destructive operations attempted?

## Relevant event categories

Senda-Argus Hooks can help collect or normalize events such as:

- `llm.request.started`
- `llm.request`
- `llm.error`
- `tool_call.requested`
- `tool_call.completed`
- `tool_call.failed`
- `mcp.tool_call.requested`
- `mcp.tool_call.completed`
- `mcp.tool_call.failed`
- `retrieval.requested`
- `retrieval.completed`
- `embedding.requested`
- `rag.query.started`
- `rag.query.completed`
- `agent.step.started`
- `agent.step.completed`
- `agent.step.failed`
- `agent.decision`

## Detection ideas

Useful detections include:

- A Langflow or agent workflow invoking tools not normally used by that flow.
- MCP calls to new or unapproved MCP servers.
- Repeated tool failures followed by a successful call to a sensitive tool.
- RAG access to unusual indexes or collections.
- Database query tools called from an unexpected agent.
- Tool arguments containing destructive verbs such as `drop`, `delete`, `truncate`, `encrypt`, or `exfiltrate`.
- Sudden increases in tool call volume, failed retries, or cross-system access.

## Privacy-safe defaults

For security monitoring, full prompt and response capture is often unnecessary and risky. The recommended default is:

- Store hashes of prompts and responses.
- Capture tool/MCP arguments only when needed and after redaction.
- Avoid storing tool/MCP result bodies by default.
- Use stable correlation IDs for trace reconstruction.
- Keep `agent_id`, `purpose_id`, and `mcp_profile_id` available for policy and alerting.

## Langflow use case

Langflow is a useful integration example because one visual flow can include LLM calls, LangChain-compatible components, RAG retrieval, MCP calls, and custom Python tools.

The recommended repository layout is:

```text
examples/langflow/
  README.md
  custom_component_senda_argus_register.py
  register_in_langflow_python_component.py
  sample_events.jsonl
```

Use the example integration to demonstrate how Senda-Argus can monitor GUI-built AI workflows without requiring every business logic component to call `audit.event()` manually.
