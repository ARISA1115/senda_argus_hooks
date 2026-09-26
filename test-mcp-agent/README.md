# test-mcp-agent

A deterministic, local-only MCP test runtime for Senda Arugus Agent Studio.

It starts a stdio MCP server and calls three tools through the real Python MCP `ClientSession`:

- `get_server_status`
- `search_cve`
- `lookup_ip`

Expected Agent Trace events include `mcp.tool_call.requested` and `mcp.tool_call.completed`.
Expected Docker Logs also include `[senda-argus-http] POST .../v1/agent-runs/ingest` and a completion status.

Register this directory in Agent Studio as a Python runtime with `agent.py` as the entrypoint.
