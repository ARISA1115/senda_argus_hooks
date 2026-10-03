"""ツールの呼び出しの結果がエラーを表すかを判別する。

MCP の tool はエラーを例外ではなく、結果の isError の印で返すことがある。計装は例外が無ければ
完了として送るため、印を別の項目で運ばないと、受け手はエラーを返した呼び出しを成功と区別できない。
結果の本文を送る設定に依らず、印だけを常に送る。
"""

from __future__ import annotations

from typing import Any


def tool_result_is_error(response: Any) -> bool | None:
    """結果のエラーの印を返す。印を判別できない形なら None を返す。

    判別できない形を False へ倒さない。倒すと、受け手は成否の分からない呼び出しを成功として扱う。
    """
    for key in ("isError", "is_error"):
        value = response.get(key) if isinstance(response, dict) else getattr(response, key, None)
        if isinstance(value, bool):
            return value
    return None
