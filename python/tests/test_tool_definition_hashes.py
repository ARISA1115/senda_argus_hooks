"""ツールの定義のダイジェストの規則と、一覧の取得の事象に載ることのテスト。

規則は JS の実装と同じで、両方のテストが同じ例 (js/tests/fixtures/mcp_tool_definition_hashes.json) を読む。
"""

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest

from senda_argus_hooks import describe_mcp_session, register, shutdown
from senda_argus_hooks.core.mcp_tools import (
    MAX_NAME_LEN,
    MAX_TOOLS_PER_SERVER,
    fold_name,
)
from senda_argus_hooks.core.tool_definitions import (
    normalize_provider_url,
    tool_definition_hash,
    tool_definition_hashes,
)

_FIXTURE = Path(__file__).resolve().parents[2] / "js" / "tests" / "fixtures" / "mcp_tool_definition_hashes.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_hash_matches_the_shared_fixture(case):
    assert tool_definition_hash(case["tool"]) == case["hash"]


def test_changed_description_changes_the_hash():
    by_name = {c["name"]: c["hash"] for c in _CASES}
    assert by_name["plain definition"] != by_name["description changed for the agent"]
    assert by_name["plain definition"] == by_name["key order does not matter"]


def test_sdk_objects_hash_like_dicts():
    class Tool:
        name = "lookup_order"
        description = "Look up an order by id."
        inputSchema: ClassVar[dict] = {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}

    class Response:
        tools: ClassVar[list] = [Tool()]

    assert tool_definition_hashes(Response()) == {"lookup_order": _CASES[0]["hash"]}


def test_hashes_are_capped_and_long_names_are_folded():
    long_name = "x" * (MAX_NAME_LEN + 1)
    tools = [{"name": long_name}] + [{"name": f"t{i}"} for i in range(MAX_TOOLS_PER_SERVER + 10)]
    hashes = tool_definition_hashes({"tools": tools})
    assert len(hashes) == MAX_TOOLS_PER_SERVER
    assert fold_name(long_name) in hashes
    assert long_name not in hashes


def test_tools_without_a_name_are_skipped():
    assert tool_definition_hashes([{"description": "no name"}, {"name": ""}]) == {}


class _RealShapedClientSession:
    """実物の ClientSession と同じく、サーバの名前も URL も属性に持たない偽物。"""

    async def call_tool(self, name, arguments=None, **kwargs):
        return {}

    async def list_tools(self):
        return {"tools": [_CASES[0]["tool"]]}


def _list_once(tmp_path, monkeypatch, session_factory):
    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = _RealShapedClientSession
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    path = tmp_path / "events.jsonl"
    register(
        project="test-mcp",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_argus_sdk=False,
        capture_result=False,
    )
    try:
        asyncio.run(session_factory().list_tools())
    finally:
        shutdown()
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    listed = [e for e in events if e["event_type"] == "mcp.list_tools.completed"]
    assert len(listed) == 1
    return listed[0]["data"]["mcp"]


def test_list_tools_event_carries_hashes_and_the_described_url(tmp_path, monkeypatch):
    """利用者が明示した URL が一覧の取得の記録に載る。JS の serverUrl と同じ入力。"""
    mcp = _list_once(
        tmp_path,
        monkeypatch,
        lambda: describe_mcp_session(
            _RealShapedClientSession(), server_url="https://MCP.EXAMPLE.com/mcp/", server_name="orders"
        ),
    )
    assert mcp["tool_definition_hashes"] == {"lookup_order": _CASES[0]["hash"]}
    assert mcp["server_url"] == "https://mcp.example.com/mcp"
    assert mcp["server"] == "orders"


def test_list_tools_url_drops_credentials_and_takes_the_provider_form(tmp_path, monkeypatch):
    """URL の資格情報は送らない。既定のポートと区切りの違いも、Argus の取得と同じ鍵へ畳む。"""
    userinfo = "user" + ":" + "pw" + "@"
    mcp = _list_once(
        tmp_path,
        monkeypatch,
        lambda: describe_mcp_session(
            _RealShapedClientSession(),
            server_url=f"https://{userinfo}MCP.EXAMPLE.com:443/a/../mcp/./",
            server_name="orders",
        ),
    )
    assert mcp["server_url"] == "https://mcp.example.com/mcp"
    assert "pw" not in json.dumps(mcp)


def test_without_a_described_url_no_url_is_sent(tmp_path, monkeypatch):
    """実物の形のセッションは URL を持たない。明示が無ければ推測した URL を載せない。"""
    mcp = _list_once(tmp_path, monkeypatch, _RealShapedClientSession)
    assert mcp["tool_definition_hashes"] == {"lookup_order": _CASES[0]["hash"]}
    assert mcp["server_url"] is None


_URLS = json.loads(_FIXTURE.read_text(encoding="utf-8"))["urls"]


@pytest.mark.parametrize("case", _URLS, ids=[c["input"] or "empty" for c in _URLS])
def test_provider_url_matches_the_shared_fixture(case):
    assert normalize_provider_url(case["input"]) == case["expected"]
