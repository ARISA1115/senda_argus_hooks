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


def test_a_nested_mutating_method_is_not_a_read() -> None:
    nested = {"request": {"url": "https://argus.example/v1/rules/r1", "method": "DELETE"}}
    assert monitor_targets(nested, tool="fetch") == ["detection_rules"]
    read = {"request": {"url": "https://argus.example/v1/rules/r1", "method": "GET"}}
    assert monitor_targets(read, tool="fetch") == []


def test_a_nested_body_is_not_a_read() -> None:
    assert monitor_targets({"op": {"path": _HOOKS_ENV, "content": ""}}, tool="read_file") == ["collector_config"]


def test_scan_truncation_marks_egress_overflow() -> None:
    from senda_argus_hooks.core.egress_hosts import MAX_SCANNED_STRINGS, egress_hosts_with_overflow

    padded = {"pad": ["x"] * MAX_SCANNED_STRINGS, "url": "https://exfil.example.net/a"}
    hosts, truncated = egress_hosts_with_overflow(padded)
    assert hosts == []
    assert truncated is True
    assert egress_hosts_with_overflow({"pad": ["x"] * 10})[1] is False


def test_scan_truncation_is_a_monitor_target() -> None:
    from senda_argus_hooks.core.monitor_targets import MAX_SCANNED_STRINGS, UNSCANNED

    padded = {"pad": ["x"] * MAX_SCANNED_STRINGS, "path": _HOOKS_ENV}
    assert monitor_targets(padded, tool="write_file") == [UNSCANNED]
    # 打ち切った先に本文や方式が在りうるため、名前が読み取りでも読み取りと言い切らない。
    assert monitor_targets(padded, tool="read_file") == [UNSCANNED]
    assert monitor_targets({"path": _HOOKS_ENV}, tool="read_file") == []


def test_a_long_string_clipped_in_the_middle_is_unscanned() -> None:
    from senda_argus_hooks.core.monitor_targets import MAX_STRING_LEN, UNSCANNED

    pad = "a" * MAX_STRING_LEN
    command = "cp x " + pad + " " + _HOOKS_ENV + " " + pad
    assert monitor_targets({"command": command}, tool="run") == [UNSCANNED]
    # 読み取りと言い切れる命令は、長くても対象に載せない。
    assert monitor_targets({"command": "cat " + pad + " " + _HOOKS_ENV + " " + pad}, tool="run") == []


def test_continuation_pages_add_read_only_tools(tmp_path, monkeypatch) -> None:
    Session = _install(monkeypatch)
    pages = {
        None: {"tools": [{"name": "read_file", "annotations": {"readOnlyHint": True}}], "nextCursor": "p2"},
        "p2": {"tools": [{"name": "view_file", "annotations": {"readOnlyHint": True}}]},
    }

    async def list_tools(self, cursor=None, *, params=None):
        return pages[cursor if cursor is not None else getattr(params, "cursor", None)]

    Session.list_tools = list_tools
    path = tmp_path / "e.jsonl"
    _register(path)
    session = Session()
    asyncio.run(session.list_tools())
    asyncio.run(session.list_tools(cursor="p2"))
    asyncio.run(session.call_tool("read_file", {"path": "/data/a.md"}))
    asyncio.run(session.list_tools(params=types.SimpleNamespace(cursor="p2")))
    asyncio.run(session.call_tool("read_file", {"path": "/data/a.md"}))
    # 新しい一覧の始まりで空にする。
    pages[None] = {"tools": [{"name": "view_file", "annotations": {"readOnlyHint": True}}]}
    asyncio.run(session.list_tools())
    asyncio.run(session.call_tool("read_file", {"path": "/data/a.md"}))
    shutdown()
    calls = [e["data"]["mcp"] for e in _events(path) if e["event_type"] == "mcp.tool_call.requested"]
    assert calls[0].get("access_direction") == "read"
    assert calls[1].get("access_direction") == "read"
    assert "access_direction" not in calls[2]


def test_a_large_body_or_bulk_rows_are_not_unscanned() -> None:
    from senda_argus_hooks.core.monitor_targets import MAX_SCANNED_STRINGS, MAX_STRING_LEN

    assert monitor_targets({"path": "/tmp/report.md", "content": "a" * (MAX_STRING_LEN + 10)}, tool="write_file") == []
    rows = [{"id": str(i), "name": "n"} for i in range(MAX_SCANNED_STRINGS)]
    assert monitor_targets({"table": "t", "rows": rows}, tool="insert_rows") == []


def test_the_read_only_set_is_capped(monkeypatch) -> None:
    from senda_argus_hooks.instrumentors import mcp_python

    monkeypatch.setattr(mcp_python, "MAX_TOOLS_PER_SERVER", 3)
    session = types.SimpleNamespace()

    def page(names):
        return {"tools": [{"name": n, "annotations": {"readOnlyHint": True}} for n in names]}

    mcp_python._record_read_only_tools(session, page(["a", "b"]), continuation=False)
    mcp_python._record_read_only_tools(session, page(["c", "d", "e"]), continuation=True)
    assert len(getattr(session, mcp_python.READ_ONLY_TOOLS_ATTR)) == 3
