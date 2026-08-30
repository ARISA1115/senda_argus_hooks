import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from senda_argus_hooks import register, shutdown


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _install_fake_mcp(monkeypatch):
    class ClientSession:
        server = "fake_mcp"
        server_url = "https://MCP.EXAMPLE.com/mcp/?token=secret"
        capability = "vulnerability_intelligence"

        async def call_tool(self, name, arguments=None, **kwargs):
            if name == "explode":
                raise RuntimeError("fake mcp failure")
            return {"ok": True, "name": name, "arguments": arguments or {}}

        async def list_tools(self):
            return [{"name": "lookup"}]

        async def read_resource(self, uri):
            return {"uri": uri}

        async def list_resources(self):
            return []

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = ClientSession
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    return fake_mcp, ClientSession


def test_mcp_python_instrumentor_emits_requested_completed_failed_and_unpatches(tmp_path, monkeypatch):
    _, ClientSession = _install_fake_mcp(monkeypatch)
    original = ClientSession.call_tool
    path = tmp_path / "events.jsonl"

    result = register(
        project="test-mcp",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_argus_sdk=False,
        capture_arguments=True,
        capture_result=False,
    )
    assert result["instrumentors"]["mcp_python"] is True
    assert ClientSession.call_tool is not original

    session = ClientSession()
    response = asyncio.run(session.call_tool("lookup", {"query": "CVE-2024-3094"}))
    assert response["ok"] is True
    with pytest.raises(RuntimeError, match="fake mcp failure"):
        asyncio.run(session.call_tool("explode", {"query": "CVE-2024-3094"}))

    shutdown()
    assert ClientSession.call_tool is original

    events = _read_events(path)
    assert [event["event_type"] for event in events] == [
        "mcp.tool_call.requested",
        "mcp.tool_call.completed",
        "mcp.tool_call.requested",
        "mcp.tool_call.failed",
    ]
    requested = events[0]
    completed = events[1]
    failed = events[3]
    assert requested["source"]["sdk"] == "mcp_python"
    assert requested["data"]["mcp"]["server"] == "fake_mcp"
    assert requested["data"]["mcp"]["server_url"] == "https://mcp.example.com/mcp"
    assert requested["data"]["mcp"]["tool"] == "lookup"
    assert requested["data"]["mcp"]["purpose_id"].startswith("purpose_")
    assert requested["data"]["mcp"]["mcp_profile_id"].startswith("mcp_profile_")
    assert completed["data"]["mcp"]["result_hash"]
    assert "result" not in completed["data"]["mcp"]
    assert failed["status"] == "error"
    assert failed["error"]["type"] == "RuntimeError"
