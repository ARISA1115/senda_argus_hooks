"""offered_alternatives が登録済み MCP ソースから mcp_server を候補へ伝播することを検証する。

素の関数名だけだと誘導検知が別 server の同一 logical 名の衝突を判別できないため、
register_mcp_tool_source で記録済みのツールは由来 server を候補に載せる。未登録は空。
"""

from __future__ import annotations

from senda_argus_hooks.core.purpose_registry import (
    offered_alternatives,
    register_mcp_tool_source,
)


def test_offered_alternatives_propagates_registered_server():
    register_mcp_tool_source(tool_name="trusted__search", mcp_server_name="trusted")
    result = offered_alternatives(["trusted__search", "plain_fn"])
    assert {"name": "trusted__search", "mcp_server": "trusted"} in result
    # MCP ソース未登録のツールは mcp_server 空で載せる。
    assert {"name": "plain_fn", "mcp_server": ""} in result


def test_offered_alternatives_non_list_returns_empty():
    assert offered_alternatives(None) == []
    assert offered_alternatives("x") == []


def test_offered_alternatives_skips_empty_names():
    result = offered_alternatives(["", "keep"])
    assert result == [{"name": "keep", "mcp_server": ""}]
