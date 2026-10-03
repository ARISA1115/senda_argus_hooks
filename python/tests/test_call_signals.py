"""MCP の呼び出しに、宛先と監視の構成要素の区分と実行環境の札と資源の向きを載せることを固定する。

引数の本文を送らない既定の構成でも、受け取り側の判定に要る正規化した値だけを送る。
"""

import asyncio
import json
import sys
import types
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core.egress_hosts import egress_hosts, normalize_host
from senda_argus_hooks.core.monitor_targets import monitor_targets
from senda_argus_hooks.core.runtime import valid_run_environment
from senda_argus_hooks.instrumentors.mcp_python import READ_RESOURCE_COMPLETED

_HOOKS_ENV = "/etc/senda-argus/" + "hooks" + ".env"


def _events(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _install(monkeypatch):
    class ClientSession:
        server = "fs"

        async def call_tool(self, name, arguments=None, **kwargs):
            return {"ok": True}

        async def list_tools(self):
            return {
                "tools": [
                    {"name": "read_file", "annotations": {"readOnlyHint": True}},
                    {"name": "delete_file", "annotations": {}},
                ]
            }

        async def read_resource(self, uri):
            return {"uri": uri}

    fake = types.ModuleType("mcp")
    fake.ClientSession = ClientSession
    monkeypatch.setitem(sys.modules, "mcp", fake)
    return ClientSession


def _register(path: Path, **kw):
    return register(
        project="t",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_argus_sdk=False,
        **kw,
    )


def test_derivations() -> None:
    assert normalize_host("HTTPS://API.Telegram.org.:443/x") == "api.telegram.org"
    assert egress_hosts({"host": "Discord.com"}) == ["discord.com"]
    assert monitor_targets({"path": _HOOKS_ENV, "content": "x"}, tool="write_file") == ["collector_config"]
    assert monitor_targets({"path": _HOOKS_ENV}, tool="read_file") == []


def test_the_run_environment_is_limited_to_the_allowed_values() -> None:
    assert valid_run_environment(" Test ") == "test"
    assert valid_run_environment("dev") is None
    assert valid_run_environment(None) is None


def test_call_meta_carries_signals_without_arguments(tmp_path, monkeypatch) -> None:
    Session = _install(monkeypatch)
    path = tmp_path / "e.jsonl"
    _register(path, capture_arguments=False, run_environment="evaluation")
    session = Session()
    asyncio.run(session.call_tool("write_file", {"path": _HOOKS_ENV, "content": "x", "host": "api.telegram.org"}))
    shutdown()
    requested = _events(path)[0]
    mcp = requested["data"]["mcp"]
    assert "arguments" not in mcp
    assert mcp["monitor_targets"] == ["collector_config"]
    assert mcp["egress_hosts"] == ["api.telegram.org"]
    assert requested["run_environment"] == "evaluation"


def test_an_unknown_run_environment_is_not_carried(tmp_path, monkeypatch) -> None:
    Session = _install(monkeypatch)
    path = tmp_path / "e.jsonl"
    _register(path, run_environment="dev")
    asyncio.run(Session().call_tool("lookup", {}))
    shutdown()
    assert _events(path)[0]["run_environment"] is None


def test_a_read_only_tool_is_classified_as_a_read(tmp_path, monkeypatch) -> None:
    Session = _install(monkeypatch)
    path = tmp_path / "e.jsonl"
    _register(path)
    session = Session()
    asyncio.run(session.list_tools())
    asyncio.run(session.call_tool("read_file", {"path": "/data/a.md"}))
    asyncio.run(session.call_tool("delete_file", {"path": "/data/a.md"}))
    asyncio.run(session.read_resource("file:///data/b.md"))
    shutdown()
    events = _events(path)
    calls = [e for e in events if e["event_type"] == "mcp.tool_call.requested"]
    assert calls[0]["data"]["mcp"]["access_direction"] == "read"
    assert "access_direction" not in calls[1]["data"]["mcp"]
    assert any(e["event_type"] == READ_RESOURCE_COMPLETED for e in events)
