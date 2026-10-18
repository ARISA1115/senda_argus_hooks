"""呼び出しの引数と戻り値から導いた IOC の項目を、事象の mcp ブロックへ載せる形にする。

導出の規則は ioc_signals にあり、受け取り側と同じ内容で置く。ここは載せる側の取りまとめだけを持つ。

**引数の本文を送らない既定でも送る。** 送るのは導いた URL とハッシュと、文脈の無い 64 桁の 16 進の
コミットメントだけで、引数や戻り値の本文は載せない。収集 hook は組織の登録を知らないため、文脈の
無い候補は値でなくコミットメントにする。

**版を常に載せる。** 該当が無い呼び出しでも版を載せる。受け取り側は版の有無で、この hook が IOC を
導出する版かどうかを区別し、古い版の hook からの無言を「該当なし」と取り違えない。
"""

from __future__ import annotations

from typing import Any

from senda_argus_hooks.core.ioc_signals import (
    IOC_SIGNALS_FIELD,
    IOC_SIGNALS_VERSION,
    ioc_signal_fields,
)


def ioc_call_fields(value: Any) -> dict[str, Any]:
    """引数か戻り値から導いた IOC の項目と版を返す。導出に失敗しても版は返し、事象は送る。"""
    out: dict[str, Any] = {IOC_SIGNALS_FIELD: IOC_SIGNALS_VERSION}
    try:
        if value is not None:
            out.update(ioc_signal_fields(value))
    except Exception:
        # 導出の失敗で呼び出しを止めない。落としたことは打ち切りの印で受け取り側へ伝える。
        out["ioc_urls_truncated"] = True
        out["ioc_hashes_truncated"] = True
    return out
