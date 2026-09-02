"""ツール呼び出しの引数から、資源の同一性と読み書きの向きを導出する。

同じ資源に対する読み取りと書き込みを、1 つの鍵で結び付けるために置く。

**``data_source_hash`` はこの用途に使えない。** あちらは提供元とツール名と capability から作るため、
同じファイルを扱っていても ``read_file`` と ``write_file`` で別の値になる。同一の資源に対する
読み書きの往復を追う鍵にはならない。ここではツール名を含めず、資源を指す引数だけから作る。

**名前そのものは送らない。** 判定に要るのは同一性だけである。名前を運ぶと、ファイルの位置や
識別子が受け取り側の権威記録へ残る。ダイジェストにして同一性だけを渡す。

**向きは引数の形から決める。** ツール名で判定しない。名前は提供元ごとに自由で、部分一致にすると
無関係な名前まで拾い、逆に取りこぼす。実際に効いているのは、本文にあたる引数が在るかどうかである。
読み取り系のツールは本文を引数に取らない。
"""

from __future__ import annotations

from typing import Any, Final, Optional

from senda_argus_hooks.core.identity import stable_hash

# 資源を指す引数の名前。提供元ごとに異なる。
_RESOURCE_KEYS: Final[tuple[str, ...]] = (
    "path", "file_path", "filename", "file", "target_path", "filepath",
    "uri", "url", "key", "object_key", "blob", "document_id", "item_id",
    "resource", "resource_id", "id",
)

# 本文にあたる引数の名前。これが在れば書き込みとみなす。
_BODY_KEYS: Final[tuple[str, ...]] = (
    "content", "text", "body", "data", "new_string", "new_str", "contents",
    "value", "file_text", "source", "patch", "diff", "payload",
)

READ: Final[str] = "read"
WRITE: Final[str] = "write"


def _first_present(arguments: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = arguments.get(key)
        if value is not None and value != "":
            return value
    return None


def resource_identity(arguments: Any) -> Optional[str]:
    """資源を指す引数から、同一性を表すダイジェストを返す。該当しなければ None。

    ツール名を含めない。含めると、同じ資源への読み取りと書き込みが別の値になり、往復を追えない。
    """
    if not isinstance(arguments, dict):
        return None
    raw = _first_present(arguments, _RESOURCE_KEYS)
    if raw is None:
        return None
    text = str(raw).strip().replace("\\", "/")
    if not text:
        return None
    return stable_hash(text, prefix="resource")


def access_direction(arguments: Any) -> Optional[str]:
    """呼び出しの向きを返す。資源を特定できない場合は None。

    本文にあたる引数が在れば書き込み、無ければ読み取りとする。ツール名は見ない。
    """
    if not isinstance(arguments, dict):
        return None
    if _first_present(arguments, _RESOURCE_KEYS) is None:
        return None
    body = _first_present(arguments, _BODY_KEYS)
    return WRITE if body is not None else READ


def classify_resource_access(arguments: Any) -> dict[str, str]:
    """資源への呼び出しなら、同一性と向きを返す。該当しなければ空の辞書。

    2 つを別々に導出すると、片方だけ載る記録が生まれる。向きだけ在って資源が無い記録は
    判定に使えず、資源だけ在って向きが無い記録は交互性を判定できない。同時に出す。
    """
    identity = resource_identity(arguments)
    if identity is None:
        return {}
    direction = access_direction(arguments)
    if direction is None:
        return {}
    return {"resource_id": identity, "access_direction": direction}
