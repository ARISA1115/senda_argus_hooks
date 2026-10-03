"""tool の戻り値から、検知の走査だけに使う文を作る。

戻り値の本文は `capture_result` を有効にしたときだけ送る。本文を送らない既定の導入では、戻り値に
埋め込まれた指示が Argus の注入の規則に一度も届かない。ここで作る文は走査のためだけに送り、
Argus は保存せず、判定へ渡した後に捨てる。本文の保存とは別の設定で切り替える。

**秘匿は平坦にする前に当てる。** 鍵名で資格情報と分かる項目は、平坦にすると鍵名との対応が
失われ、値だけが残る。辿りながら鍵名で伏せ、文字列には形式の秘匿を当てる。

**辿る深さには上限を置き、再帰を使わない。** 戻り値はサーバが決める値で、深い入れ子を返すだけで
再帰の上限に当たり、事象そのものが送られなくなる。上限を超えた部分は落とし、落としたことを印で
示す。

**長さには上限を置く。** 上限を超えた文は先頭と末尾を残して中央を落とす。先頭だけを残すと、
長い前置きの後ろへ指示を置くだけで走査から外せる。切り詰めたことと元の長さを印で示す。
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from senda_argus_hooks.core.redaction import DEFAULT_REDACT_FIELDS, _redact_str

# 走査の文の長さの上限。先頭と末尾に半分ずつ割り当てる。
RESULT_SCAN_MAX_CHARS: int = 32_768
# 中央を落としたときに先頭と末尾の間へ置く区切り。両側の語が癒着して別の語に見えないようにする。
RESULT_SCAN_ELISION: str = "\n...\n"
# 辿る入れ子の深さの上限。
RESULT_SCAN_MAX_DEPTH: int = 32
# 事象へ載せる項目の名前。
SCAN_FIELD = "result_scan"
TRUNCATED_FIELD = "result_scan_truncated"
LENGTH_FIELD = "result_scan_length"
FAILED_FIELD = "result_scan_failed"

_REDACTED = "***REDACTED***"

# 文字列の中に書かれた鍵と値の組。構造を解析できない応答や、辞書を文字列にした値でも、鍵名で
# 資格情報と分かる値を伏せる。鍵の前は語の続きでないこと、引用符は前に逆斜線があってもよい。
_KEYS = "|".join(re.escape(k) for k in sorted(DEFAULT_REDACT_FIELDS, key=len, reverse=True))
_KV_PATTERN = re.compile(
    r"(^|[^A-Za-z0-9_-])(\\?[\"']?)(" + _KEYS + r")(\\?[\"']?)(\s*[:=]\s*)"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s,;&}\]]+)",
    re.IGNORECASE,
)


def redact_scan_string(value: str) -> str:
    """走査の文に入れる文字列から、形式で分かる秘密と、文字列の中の鍵と値の組の値を伏せる。"""
    return _KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}{m.group(5)}\"{_REDACTED}\"", _redact_str(value))


def scan_source(response: Any) -> Any:
    """走査に渡す値を、鍵名が残る構造のまま取り出す。

    辞書を 1 つの文字列にしてから渡すと、鍵名による秘匿が効かず短い秘密がそのまま送られる。
    構造を取り出せない値だけを文字列にし、その場合も文字列の中の鍵と値の組を伏せる。
    """
    if response is None or isinstance(response, (str, dict, list, tuple)):
        return response
    for attr in ("model_dump", "dict", "json"):
        if hasattr(response, attr):
            value = None
            with contextlib.suppress(Exception):
                value = getattr(response, attr)()
            if isinstance(value, str):
                # json の文字列を返す応答は解析して鍵名を取り戻す。解析できなければ文字列のまま渡し、
                # 文字列の中の鍵と値の組を伏せる。
                try:
                    value = json.loads(value)
                except ValueError:
                    return value
            if isinstance(value, (dict, list, tuple, str)):
                return value
    return str(response)


def _collect(value: Any) -> tuple[list[str], bool]:
    """値の中の文字列を順に集める。辞書は鍵も集める。深さの上限を超えたかも返す。

    Argus の走査も辞書の鍵を読む。鍵を落とすと、構造化した戻り値の鍵へ置いた指示が届かない。
    """
    parts: list[str] = []
    cut = False
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            parts.append(redact_scan_string(item))
            continue
        if not isinstance(item, (dict, list, tuple)):
            continue
        if depth >= RESULT_SCAN_MAX_DEPTH:
            cut = True
            continue
        children: list[tuple[Any, int]] = []
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str):
                    children.append((key, depth + 1))
                    if key.lower() in DEFAULT_REDACT_FIELDS:
                        children.append((_REDACTED, depth + 1))
                        continue
                children.append((child, depth + 1))
        else:
            children = [(child, depth + 1) for child in item]
        # 後に積んだものから取り出すため、逆順に積んで元の並びを保つ。
        stack.extend(reversed(children))
    return parts, cut


def result_scan_text(value: Any) -> str | None:
    """戻り値から走査の文を作る。文字列を 1 つも含まなければ None を返す。"""
    return result_scan_fields(value).get(SCAN_FIELD)


def result_scan_fields(value: Any) -> dict[str, Any]:
    """事象へ載せる走査の文と印を返す。載せるものが無ければ空を返す。

    作成に失敗しても例外を出さず、落としたことを示す印だけを返す。事象は送る。
    """
    return _scan_fields(value, SCAN_FIELD, TRUNCATED_FIELD, LENGTH_FIELD, FAILED_FIELD)


# 推論へ渡った入力の走査の文を載せる項目の名前。音声のセッションの発話の文字起こしがここへ載る。
# 戻り値の走査の文と同じく、Argus は保存せず判定の後に捨てる。
INPUT_SCAN_FIELD = "input_scan"
INPUT_TRUNCATED_FIELD = "input_scan_truncated"
INPUT_LENGTH_FIELD = "input_scan_length"
INPUT_FAILED_FIELD = "input_scan_failed"


def input_scan_fields(value: Any) -> dict[str, Any]:
    """推論の入力から、走査の文と印を作る。秘匿と上限は戻り値の走査の文と同じ実装を通す。

    導出を 2 つ持たない。入力の側だけ別に書くと、秘匿か上限の片方だけが変わったときに、
    文字起こしだけ秘密が通るか、長い前置きの後ろの指示が落ちる。
    """
    return _scan_fields(
        value, INPUT_SCAN_FIELD, INPUT_TRUNCATED_FIELD, INPUT_LENGTH_FIELD, INPUT_FAILED_FIELD
    )


def _scan_fields(
    value: Any, scan_field: str, truncated_field: str, length_field: str, failed_field: str
) -> dict[str, Any]:
    try:
        parts, cut = _collect(value)
        parts = [p for p in parts if p.strip()]
        if not parts:
            return {truncated_field: True} if cut else {}
        text = " ".join(parts)
        out: dict[str, Any] = {}
        if len(text) > RESULT_SCAN_MAX_CHARS:
            out[truncated_field] = True
            out[length_field] = len(text)
            keep = (RESULT_SCAN_MAX_CHARS - len(RESULT_SCAN_ELISION)) // 2
            text = text[:keep] + RESULT_SCAN_ELISION + text[-keep:]
        elif cut:
            out[truncated_field] = True
        out[scan_field] = text
        return out
    except Exception:  # noqa: BLE001
        return {failed_field: True}
