from __future__ import annotations

from typing import Any, Callable, Mapping


class MockMCPClient:
    """Tiny SDK-style MCP client for local mock tools.

    This class intentionally contains no Argus logging code. The SDK
    instrumentor wraps call_tool() and emits mcp.tool_call / mcp.error events.
    """

    def __init__(self, tools: Mapping[str, Callable[..., Any]], *, server: str = "mock_mcp"):
        self.tools = dict(tools)
        self.server = server

    def call_tool(self, tool: str, arguments: dict[str, Any] | None = None, *, capability: str | None = None) -> Any:
        args = arguments or {}
        fn = self.tools.get(tool)
        if fn is None:
            raise KeyError(f"mock tool not implemented: {tool}")
        # Existing PromptOps mock tools accept query as a positional argument.
        if "query" in args and len(args) == 1:
            return fn(args["query"])
        return fn(**args)
