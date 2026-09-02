"""資源の同一性と読み書きの向きの導出を固定する。

同じ資源への読み取りと書き込みが 1 つの鍵で結び付くことが要点である。既存の data_source_hash は
ツール名を含むため、同じ資源でも読み取りと書き込みで別の値になり、往復を追う鍵にならない。
"""

from __future__ import annotations

from senda_argus_hooks.core.identity import data_source_hash, mcp_data_source_profile
from senda_argus_hooks.core.resource_access import (
    READ,
    WRITE,
    access_direction,
    classify_resource_access,
    resource_identity,
)


class TestIdentity:
    def test_same_resource_gives_the_same_key_for_read_and_write(self) -> None:
        r = classify_resource_access({"path": "/data/notes.md"})
        w = classify_resource_access({"path": "/data/notes.md", "content": "x"})
        assert r["resource_id"] == w["resource_id"]
        assert r["access_direction"] == READ
        assert w["access_direction"] == WRITE

    def test_data_source_hash_cannot_serve_as_this_key(self) -> None:
        """既存の鍵はツール名を含むため、同じ資源でも読み書きで値が分かれる。

        本実装を置いた理由をここで固定する。既存の鍵で足りるなら重複になる。
        """
        read_profile = mcp_data_source_profile(mcp_server_name="s", tool_name="read_file")
        write_profile = mcp_data_source_profile(mcp_server_name="s", tool_name="write_file")
        assert data_source_hash(read_profile) != data_source_hash(write_profile)

    def test_different_resources_do_not_collide(self) -> None:
        a = resource_identity({"path": "/data/a.md"})
        b = resource_identity({"path": "/data/b.md"})
        assert a is not None and b is not None and a != b

    def test_separator_is_normalized(self) -> None:
        assert resource_identity({"path": "C:\\data\\a.md"}) == resource_identity(
            {"path": "C:/data/a.md"}
        )

    def test_the_name_itself_is_not_carried(self) -> None:
        """名前そのものを載せないこと。判定に要るのは同一性だけである。"""
        out = classify_resource_access({"path": "/secret/customer-list.csv"})
        assert "customer-list" not in str(out)
        assert out["resource_id"].startswith("resource_")


class TestDirection:
    def test_a_body_argument_marks_a_write(self) -> None:
        assert access_direction({"path": "/a", "content": "x"}) == WRITE

    def test_absence_of_a_body_marks_a_read(self) -> None:
        assert access_direction({"path": "/a"}) == READ

    def test_direction_does_not_depend_on_the_tool_name(self) -> None:
        """ツール名で判定しないこと。

        名前は提供元ごとに自由で、部分一致にすると無関係な名前を拾い、逆に取りこぼす。
        """
        assert access_direction({"tool": "read_file", "path": "/a", "content": "x"}) == WRITE


class TestNotApplicable:
    def test_no_resource_argument_yields_nothing(self) -> None:
        assert classify_resource_access({"query": "select 1"}) == {}

    def test_non_dict_yields_nothing(self) -> None:
        assert classify_resource_access(None) == {}
        assert classify_resource_access("path") == {}

    def test_empty_resource_value_yields_nothing(self) -> None:
        assert classify_resource_access({"path": ""}) == {}

    def test_identity_and_direction_are_emitted_together(self) -> None:
        """片方だけ載る記録を作らないこと。

        向きだけでは判定に使えず、資源だけでは交互性を判定できない。
        """
        out = classify_resource_access({"path": "/a"})
        assert set(out) == {"resource_id", "access_direction"}
