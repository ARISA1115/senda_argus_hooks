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


_MISSING: Final[object] = object()


def _first_present(arguments: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """最初に見つかった値を返す。値が空でも「在る」として返す。

    鍵の不在と、鍵は在って値が空であることを区別する。区別しないと、資源を空にする書き込みが
    本文の無い呼び出しに見え、読み取りとして記録される。資源を空にするのは破壊的な操作であり、
    向きを取り違えると監査の意味が反転する。
    """
    for key in keys:
        if key in arguments and arguments[key] is not None:
            return arguments[key]
    return _MISSING


def resource_identity(arguments: Any, *, server: str | None = None) -> Optional[str]:
    """資源を指す引数から、同一性を表すダイジェストを返す。該当しなければ None。

    ツール名を含めない。含めると、同じ資源への読み取りと書き込みが別の値になり、往復を追えない。
    """
    if not isinstance(arguments, dict):
        return None
    raw = _first_present(arguments, _RESOURCE_KEYS)
    if raw is _MISSING:
        return None
    text = str(raw).strip().replace("\\", "/")
    # 資源は空文字を許さない。指す先が定まらないため同一性の鍵にならない
    if not text:
        return None
    if server:
        # 提供元が違えば同じ名前でも別の資源である。提供元を含めないと、よくある名前や
        # 短い識別子が別のサーバの資源と同じ鍵になり、無関係な読み書きが往復に見える。
        # ツール名は含めない。含めると同じ資源への読み取りと書き込みが別の鍵になる。
        text = f"{str(server).strip()}\x00{text}"
    return stable_hash(text, prefix="resource")


def access_direction(arguments: Any) -> Optional[str]:
    """呼び出しの向きを返す。資源を特定できない場合は None。

    本文にあたる引数が在れば書き込み、無ければ読み取りとする。ツール名は見ない。
    """
    if not isinstance(arguments, dict):
        return None
    if _first_present(arguments, _RESOURCE_KEYS) is _MISSING:
        return None
    # 本文の鍵が在れば、値が空でも書き込みとする。空の本文は資源を空にする操作である
    return WRITE if _first_present(arguments, _BODY_KEYS) is not _MISSING else READ


def classify_resource_access(arguments: Any, *, server: str | None = None) -> dict[str, str]:
    """資源への呼び出しなら、同一性と向きを返す。該当しなければ空の辞書。

    2 つを別々に導出すると、片方だけ載る記録が生まれる。向きだけ在って資源が無い記録は
    判定に使えず、資源だけ在って向きが無い記録は交互性を判定できない。同時に出す。
    """
    identity = resource_identity(arguments, server=server)
    if identity is None:
        return {}
    direction = access_direction(arguments)
    if direction is None:
        return {}
    return {"resource_id": identity, "access_direction": direction}


def classify_read_resource(args: Any, kwargs: Any, *, server: str | None = None) -> dict[str, str]:
    """資源の直接読み取りを分類する。

    MCP の資源読み取りは、ツール呼び出しと引数の形が違う。位置引数または `uri` に資源を指す値が
    直接入り、本文にあたる引数は持たない。この経路を通さないと、同じ資源への読み取りが
    ツール呼び出しによる書き込みと結び付かず、往復として現れない。
    """
    raw: Any = _MISSING
    if isinstance(kwargs, dict):
        for key in ("uri", "url", "resource", "path"):
            if key in kwargs and kwargs[key] is not None:
                raw = kwargs[key]
                break
    if raw is _MISSING and isinstance(args, (list, tuple)) and args:
        raw = args[0]
    if raw is _MISSING:
        return {}
    identity = resource_identity({"uri": raw}, server=server)
    if identity is None:
        return {}
    # 資源の直接読み取りは本文を持たない。常に読み取りである
    return {"resource_id": identity, "access_direction": READ}
