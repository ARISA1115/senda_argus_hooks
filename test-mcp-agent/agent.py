from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from senda_argus_hooks import flush


async def main() -> None:
    # The child MCP server is intentionally not instrumented.  We want this runtime's
    # ClientSession.call_tool() calls to be the observed boundary in Agent Trace.
    child_env = dict(os.environ)
    child_env["SENDA_ARGUS_ENABLED"] = "false"
    server = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(os.path.dirname(__file__), "server.py")],
        env=child_env,
    )

    print("[test-mcp-agent] starting MCP trace test", flush=True)
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            cases = [
                ("get_server_status", {}),
                ("search_cve", {"cve_id": "CVE-2024-3094"}),
                ("lookup_ip", {"ip": "203.0.113.10"}),
            ]
            for name, arguments in cases:
                result = await session.call_tool(name, arguments)
                print(f"[test-mcp-agent] tool={name} ok result_type={type(result).__name__}", flush=True)

    # Force queued audit events into the Argus exporter before process exit.
    flush()
    print("[test-mcp-agent] completed; open Agent Trace and Docker Logs", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
