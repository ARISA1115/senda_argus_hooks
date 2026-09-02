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
    classify_read_resource,
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


class TestEmptyBody:
    def test_an_empty_body_is_still_a_write(self) -> None:
        """本文の鍵が在れば、値が空でも書き込みとすること。

        資源を空にする操作を読み取りとして記録すると、破壊的な操作の向きが反転する。鍵の不在と
        値が空であることを区別する。
        """
        assert access_direction({"path": "/a", "content": ""}) == WRITE

    def test_absence_of_the_body_key_is_a_read(self) -> None:
        assert access_direction({"path": "/a"}) == READ


class TestServerScope:
    def test_the_same_name_on_different_servers_differs(self) -> None:
        """提供元が違えば同じ名前でも別の鍵になること。

        よくある名前や短い識別子が別の提供元の資源と同じ鍵になると、無関係な読み書きが往復に
        見える。
        """
        a = classify_resource_access({"path": "/data/config.json"}, server="s1")
        b = classify_resource_access({"path": "/data/config.json"}, server="s2")
        assert a["resource_id"] != b["resource_id"]

    def test_the_same_resource_on_one_server_matches(self) -> None:
        r = classify_resource_access({"path": "/a"}, server="s1")
        w = classify_resource_access({"path": "/a", "content": "x"}, server="s1")
        assert r["resource_id"] == w["resource_id"]


class TestReadResource:
    def test_a_positional_uri_is_classified(self) -> None:
        """資源の直接読み取りは引数の形が違う。位置引数の値を資源として扱う。"""
        out = classify_read_resource(("file:///data/a.md",), {}, server="s1")
        assert out["access_direction"] == READ
        assert out["resource_id"].startswith("resource_")

    def test_a_keyword_uri_is_classified(self) -> None:
        assert classify_read_resource((), {"uri": "s3://b/k"}, server="s1")["access_direction"] == READ

    def test_it_shares_the_key_with_a_tool_write(self) -> None:
        """直接読み取りと、同じ資源へのツール経由の書き込みが同じ鍵になること。

        通さないと、資源の読み取りが書き込みと結び付かず往復として現れない。
        """
        r = classify_read_resource(("file:///a",), {}, server="s1")
        w = classify_resource_access({"uri": "file:///a", "content": "x"}, server="s1")
        assert r["resource_id"] == w["resource_id"]

    def test_no_argument_yields_nothing(self) -> None:
        assert classify_read_resource((), {}, server="s1") == {}


class TestInstrumentation:
    def test_the_read_resource_path_is_wired(self) -> None:
        """資源の直接読み取りの経路が実際に分類を呼ぶこと。

        導出を置いただけでは、送出経路が呼んでいるかは分からない。
        """
        import inspect

        from senda_argus_hooks.instrumentors import mcp_python

        src = inspect.getsource(mcp_python)
        assert "classify_read_resource" in src
        assert "classify_resource_access" in src
