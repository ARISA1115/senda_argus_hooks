"""MCP のサーバ名を、計装の間で同じ規則で読むことを固定する。

読む属性が計装ごとに違うと、同じセッションが計装によって別のサーバ名で記録される。Argus は
記録したサーバ名を承認の候補に並べ、承認もサーバ名で判定するため、承認したサーバと観測した
サーバが一致しなくなる。名前を持たないセッションの既定の名前は、Argus が資産として記録しない
値と一致させる。
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core.identity import UNNAMED_MCP_SERVER, resolve_mcp_server_name


def _obj(**attrs):
    return types.SimpleNamespace(**attrs)


@pytest.mark.parametrize(
    "attrs,expected",
    [
        ({"server": "s", "server_name": "sn", "name": "n"}, "s"),
        ({"server_name": "sn", "name": "n"}, "sn"),
        ({"name": "n"}, "n"),
        ({"server": "", "server_name": None, "name": "n"}, "n"),
        ({}, UNNAMED_MCP_SERVER),
        ({"server": "", "server_name": ""}, UNNAMED_MCP_SERVER),
    ],
)
def test_the_server_name_is_read_in_one_order(attrs, expected):
    assert resolve_mcp_server_name(_obj(**attrs)) == expected


def test_the_unnamed_value_matches_what_argus_does_not_record():
    # Argus の detection-core は同じ値を資産として記録しない。値を変えるときは両方を変える。
    assert UNNAMED_MCP_SERVER == "unknown"


def _install_fake_mcp(monkeypatch, session_cls):
    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = session_cls
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)


def _emitted_server(tmp_path: Path, monkeypatch, session_cls) -> str:
    _install_fake_mcp(monkeypatch, session_cls)
    path = tmp_path / "events.jsonl"
    register(
        project="test-mcp-name",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_argus_sdk=False,
        capture_arguments=True,
        capture_result=False,
    )
    try:
        asyncio.run(session_cls().call_tool("lookup", {"query": "q"}))
    finally:
        shutdown()
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    completed = [e for e in events if e["event_type"] == "mcp.tool_call.completed"]
    assert completed
    return completed[0]["data"]["mcp"]["server"]


def test_a_session_named_only_by_name_is_emitted_with_that_name(tmp_path, monkeypatch):
    class ClientSession:
        name = "named-by-name"

        async def call_tool(self, name, arguments=None, **kwargs):
            return {"ok": True}

    assert _emitted_server(tmp_path, monkeypatch, ClientSession) == "named-by-name"


def test_a_session_without_a_name_is_emitted_with_the_unnamed_value(tmp_path, monkeypatch):
    class ClientSession:
        async def call_tool(self, name, arguments=None, **kwargs):
            return {"ok": True}

    assert _emitted_server(tmp_path, monkeypatch, ClientSession) == UNNAMED_MCP_SERVER


def test_the_sdk_mcp_client_reads_the_name_with_the_same_rule(monkeypatch):
    """SDK の MCP クライアントを包む計装も、同じ関数でサーバ名を読む。"""
    from senda_argus_hooks.instrumentors import argus_sdk

    calls = []
    real = argus_sdk.resolve_mcp_server_name

    def spy(obj):
        calls.append(obj)
        return real(obj)

    monkeypatch.setattr(argus_sdk, "resolve_mcp_server_name", spy)
    instrumentor = argus_sdk.ArgusSDKInstrumentor()
    client = _obj(server="", tools={})
    wrapped = instrumentor._wrap_mcp(lambda obj, *a, **k: {"ok": True}, "mcp.call_tool")
    wrapped(client, "lookup", {"query": "q"})
    assert calls == [client]
