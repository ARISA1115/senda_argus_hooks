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


def _install_fake_mcp_with_meta(monkeypatch):
    seen = {}

    class ClientSession:
        server = "fake_mcp"
        server_url = "https://mcp.example.com/mcp"

        async def call_tool(self, name, arguments=None, read_timeout_seconds=None, progress_callback=None, *, meta=None):
            seen["meta"] = meta
            return {"ok": True}

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = ClientSession
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    return ClientSession, seen


def _register(path):
    return register(
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


def test_call_tool_carries_one_call_id_in_the_request_meta_and_the_record(tmp_path, monkeypatch):
    ClientSession, seen = _install_fake_mcp_with_meta(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)
    asyncio.run(ClientSession().call_tool("lookup", {"q": 1}, meta={"other": "kept"}))
    shutdown()
    sent = seen["meta"]["argus/call_id"]
    assert sent.startswith("call_") and seen["meta"]["other"] == "kept"
    requested = [e for e in _read_events(path) if e["event_type"] == "mcp.tool_call.requested"]
    assert requested and requested[0]["data"]["mcp"]["call_id"] == sent


def test_a_well_formed_call_id_from_the_caller_is_reused(tmp_path, monkeypatch):
    ClientSession, seen = _install_fake_mcp_with_meta(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)
    asyncio.run(ClientSession().call_tool("lookup", {}, meta={"argus/call_id": "call_caller_0001"}))
    shutdown()
    assert seen["meta"]["argus/call_id"] == "call_caller_0001"


def test_a_session_that_does_not_accept_meta_is_called_unchanged(tmp_path, monkeypatch):
    seen = {}

    class ClientSession:
        server = "fake_mcp"
        server_url = "https://mcp.example.com/mcp"

        async def call_tool(self, name, arguments=None):
            seen["called"] = True
            return {"ok": True}

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = ClientSession
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    path = tmp_path / "events.jsonl"
    _register(path)
    assert asyncio.run(ClientSession().call_tool("lookup", {}))["ok"] is True
    shutdown()
    requested = [e for e in _read_events(path) if e["event_type"] == "mcp.tool_call.requested"]
    assert seen["called"] and "call_id" not in requested[0]["data"]["mcp"]
