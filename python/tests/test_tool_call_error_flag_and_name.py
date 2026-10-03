"""完了の記録が、結果のエラーの印と tool 名を運ぶことを固定する。

受け手は完了の記録を実行の成功として数える。MCP の tool はエラーを結果の印で返すことがあり、
結果の本文を送らない既定の設定では、印を別の項目で運ばないと成否を区別できない。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core.tool_result import tool_result_is_error
from senda_argus_hooks.integrations.openai_agents import SendaArgusOpenAIAgentsProcessor


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _install_fake_mcp(monkeypatch):
    class ClientSession:
        server = "fake_mcp"

        async def call_tool(self, name, arguments=None, **kwargs):
            if name == "denied":
                return {"isError": True, "content": []}
            if name == "plain":
                return "no flag"
            return {"isError": False, "content": []}

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = ClientSession
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    return ClientSession


def test_mcp_completion_carries_the_error_flag_without_the_result(tmp_path, monkeypatch):
    ClientSession = _install_fake_mcp(monkeypatch)
    path = tmp_path / "events.jsonl"
    register(
        project="t",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_argus_sdk=False,
        capture_result=False,
    )
    session = ClientSession()
    for name in ("approve", "denied", "plain"):
        asyncio.run(session.call_tool(name, {}))
    shutdown()
    completed = [e for e in _events(path) if e["event_type"] == "mcp.tool_call.completed"]
    assert [e["data"]["mcp"].get("is_error") for e in completed] == [False, True, None]
    assert all("result" not in e["data"]["mcp"] for e in completed)


def test_unknown_shape_is_not_reported_as_success():
    assert tool_result_is_error("text") is None
    assert tool_result_is_error({"isError": "yes"}) is None
    assert tool_result_is_error(types.SimpleNamespace(isError=True)) is True


def _span(name: str, *, error=None):
    return types.SimpleNamespace(
        type="tool", span_data=types.SimpleNamespace(type="function", name=name), error=error
    )


def test_openai_agents_tool_span_carries_the_tool_name(tmp_path):
    path = tmp_path / "events.jsonl"
    register(project="t", exporters=[{"type": "jsonl", "path": str(path)}])
    processor = SendaArgusOpenAIAgentsProcessor()
    processor.on_span_start(_span("reset_password"))
    processor.on_span_end(_span("reset_password"))
    shutdown()
    events = _events(path)
    assert [e["event_type"] for e in events] == ["tool_call.requested", "tool_call.completed"]
    assert all(e["data"]["tool"]["tool_name"] == "reset_password" for e in events)


def test_openai_agents_failed_tool_span_is_not_a_completion(tmp_path):
    path = tmp_path / "events.jsonl"
    register(project="t", exporters=[{"type": "jsonl", "path": str(path)}])
    SendaArgusOpenAIAgentsProcessor().on_span_end(_span("approve_reset", error={"message": "denied"}))
    shutdown()
    (event,) = _events(path)
    assert event["event_type"] == "tool_call.failed"
    assert event["status"] == "error"


def test_openai_agents_span_without_name_carries_no_tool(tmp_path):
    path = tmp_path / "events.jsonl"
    register(project="t", exporters=[{"type": "jsonl", "path": str(path)}])
    SendaArgusOpenAIAgentsProcessor().on_span_end(types.SimpleNamespace(type="tool"))
    shutdown()
    (event,) = _events(path)
    assert "tool" not in event["data"]
