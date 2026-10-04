"""MCP の提供元から受け取った内容を控え、指示ファイルへの書き込みのうち外部から来た部分に印を付ける。

受信した文書やメールに埋めた命令で、エージェントが自分の指示ファイルへ偽の情報を書き、次の起動から
誤った行動へ誘導される手口がある。受け取り側の伝播の判定は、主体をまたぐかどうかで記憶の更新と
伝播を分けているため、同じ主体の中で完結する汚染を記憶の更新として扱う。汚染かどうかを決めるのは、
書いた主体ではなく、書かれた内容がどこから来たかである。

**本文を運ばない。** 受け取った内容は、指示ファイルの書き込みと同じ規則で作る行と語の組の
ダイジェストとしてだけ控える。書き込みのダイジェストのうち、控えに在るものを外部から来たものとして
送る。本文は控えにも記録にも残さない。

**自分の指示ファイルの読み取りは外部の内容として控えない。** エージェントは自分の指示ファイルを
読んで書き足し、全体を書き戻す。読んだ内容を外部の内容として控えると、既に在った行がすべて外部から
来たことになり、普段の記憶の更新が汚染に見える。除外するのは、応答の行のうち、手元に実在する
その指示ファイルの今の中身にも在る行だけである。名前だけで除外すると、提供元が資源や引数に指示
ファイルの名前を付けるだけで控えを止められる。手元の中身と突き合わせれば、提供元が返した別の内容は
除外されない。エラーの印が立った応答も、本文はモデルへ渡るため控える。

**控えに収まらなかったことを黙って忘れない。** 受け取った内容が上限を超えたときと、容量で古い控えを
落としたときは、外部から来たかどうかを判別できない期間に入る。その期間の書き込みは全体を外部から来た
ものとして印を付け、判別できなかったことも印で示す。検知しない側へ倒すと、埋め草の多い文書を送るか、
文書を何通も送るだけで、本命の内容を控えの外へ押し出せる。

控えは主体ごとに分ける。主体の識別子は事象に載るものと同じ決め方で求める。期限と容量は手元の
時計で数える。この控えは計装の内側にあり、送り手の申告は入らない。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from senda_argus_hooks.core.instruction_files import (
    MAX_WRITE_DIGESTS,
    line_digests_with_overflow,
    token_pair_digests_with_overflow,
)

# 受け取った内容の控えを保つ期間。受け取り側の伝播の証拠と同じく、次の起動をまたいで成立する。
LEDGER_TTL_SEC: Final[float] = 86400.0
# 主体ごとに控えるダイジェストの数の上限。
MAX_DIGESTS_PER_SCOPE: Final[int] = 65536
# 控える主体の数の上限。
MAX_SCOPES: Final[int] = 64
# 1 件の応答から読む文字列の数と、入れ子の深さの上限。
MAX_SCANNED_STRINGS: Final[int] = 4096
MAX_SCAN_DEPTH: Final[int] = 32

# 送る項目の名前。
EXTERNAL_LINES: Final[str] = "written_external_line_hashes"
EXTERNAL_PAIRS: Final[str] = "written_external_pair_hashes"
ORIGIN_UNDETERMINED: Final[str] = "written_origin_undetermined"


def _strings(value: Any) -> tuple[list[str], bool]:
    """値の中の文字列を集める。上限で打ち切ったかも返す。再帰を使わない。"""
    out: list[str] = []
    cut = False
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            if len(out) >= MAX_SCANNED_STRINGS:
                return out, True
            out.append(item)
            continue
        if not isinstance(item, (dict, list, tuple)):
            continue
        if depth >= MAX_SCAN_DEPTH:
            cut = True
            continue
        children = list(item.values()) if isinstance(item, dict) else list(item)
        stack.extend((child, depth + 1) for child in reversed(children))
    return out, cut


def content_digests(value: Any) -> tuple[set[str], bool]:
    """受け取った内容の行と語の組のダイジェストと、上限に収まらなかったかを返す。"""
    texts, cut = _strings(value)
    # **上限は文字列ごとでなく全体に掛ける。** 文字列ごとに上限まで集めると、短い文字列を多数
    # 並べるだけで保持が文字列の数に比例して膨らむ。行と語の組のそれぞれに全体の枠を持ち、
    # 残りの枠を各呼び出しへ渡す。枠を使い切ったら打ち切りの印を立てて終える。
    lines: set[str] = set()
    pairs: set[str] = set()
    overflow = cut
    for text in texts:
        line_room = MAX_WRITE_DIGESTS - len(lines)
        pair_room = MAX_WRITE_DIGESTS - len(pairs)
        if line_room <= 0 or pair_room <= 0:
            overflow = True
            break
        got_lines, lines_over = line_digests_with_overflow(text, limit=line_room)
        got_pairs, pairs_over = token_pair_digests_with_overflow(text, limit=pair_room)
        lines.update(got_lines)
        pairs.update(got_pairs)
        if lines_over or pairs_over:
            overflow = True
            break
    return lines | pairs, overflow


class _Scope:
    __slots__ = ("digests", "undetermined_at")

    def __init__(self) -> None:
        self.digests: OrderedDict[str, float] = OrderedDict()
        self.undetermined_at: float | None = None



# 手元の指示ファイルを読む長さの上限。超えるファイルは除外に使わない。
MAX_LOCAL_FILE_BYTES: Final[int] = 1_048_576


def local_instruction_digests(raw: str | None) -> frozenset[str]:
    """手元に実在する指示ファイルの今の中身のダイジェストを返す。読めなければ空を返す。

    URL や手元に無い場所は読まない。提供元が名乗った名前ではなく、手元のファイルの中身で除外を決める。
    """
    if not raw:
        return frozenset()
    text = raw.strip()
    if text.lower().startswith("file://"):
        text = text[len("file://"):]
    elif "://" in text:
        return frozenset()
    try:
        path = Path(text).expanduser()
        if not path.is_file() or path.stat().st_size > MAX_LOCAL_FILE_BYTES:
            return frozenset()
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return frozenset()
    digests, _overflow = content_digests(content)
    return frozenset(digests)


class ExternalContentLedger:
    """主体ごとに、受け取った内容のダイジェストを期限付きで控える。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._lock = threading.Lock()
        self._scopes: OrderedDict[str, _Scope] = OrderedDict()
        self._clock = clock
        # 主体の数の上限で控えを丸ごと押し出した最後の時刻。押し出された主体の書き込みは、期限の間は
        # 出所を判別できないものとして扱う。黙って忘れると、主体を増やすだけで控えを消せる。
        self._evicted_at: float | None = None

    def _expire(self, scope: _Scope, now: float) -> None:
        while scope.digests:
            oldest = next(iter(scope.digests))
            if now - scope.digests[oldest] <= LEDGER_TTL_SEC:
                break
            scope.digests.popitem(last=False)
        if scope.undetermined_at is not None and now - scope.undetermined_at > LEDGER_TTL_SEC:
            scope.undetermined_at = None

    def record(self, scope_key: str, value: Any, *, exclude: frozenset[str] = frozenset()) -> None:
        """受け取った内容を控える。exclude に在るダイジェストは控えない。"""
        digests, overflow = content_digests(value)
        digests -= exclude
        if not digests and not overflow:
            return
        now = self._clock()
        with self._lock:
            scope = self._scopes.pop(scope_key, None) or _Scope()
            self._scopes[scope_key] = scope
            while len(self._scopes) > MAX_SCOPES:
                self._scopes.popitem(last=False)
                self._evicted_at = now
            self._expire(scope, now)
            for digest in digests:
                scope.digests.pop(digest, None)
                scope.digests[digest] = now
            if len(scope.digests) > MAX_DIGESTS_PER_SCOPE:
                # 容量で古い控えを落とす。落とした内容が後で書かれても外部から来たと分からない。
                while len(scope.digests) > MAX_DIGESTS_PER_SCOPE:
                    scope.digests.popitem(last=False)
                overflow = True
            if overflow:
                scope.undetermined_at = now

    def classify(self, scope_key: str, written: dict[str, Any]) -> dict[str, Any]:
        """書き込みのダイジェストのうち、外部から来たものを返す。該当しなければ空を返す。"""
        lines = [d for d in written.get("written_line_hashes") or [] if isinstance(d, str)]
        pairs = [d for d in written.get("written_pair_hashes") or [] if isinstance(d, str)]
        if not lines and not pairs:
            return {}
        now = self._clock()
        with self._lock:
            scope = self._scopes.get(scope_key)
            evicted = self._evicted_at is not None and now - self._evicted_at <= LEDGER_TTL_SEC
            if scope is None and not evicted:
                return {}
            if scope is not None:
                self._expire(scope, now)
            undetermined = evicted or (scope is not None and scope.undetermined_at is not None)
            known = scope.digests if scope is not None else {}
            ext_lines = list(lines) if undetermined else [d for d in lines if d in known]
            ext_pairs = list(pairs) if undetermined else [d for d in pairs if d in known]
        out: dict[str, Any] = {}
        if ext_lines:
            out[EXTERNAL_LINES] = ext_lines
        if ext_pairs:
            out[EXTERNAL_PAIRS] = ext_pairs
        if undetermined:
            out[ORIGIN_UNDETERMINED] = True
        return out

    def clear(self) -> None:
        with self._lock:
            self._scopes.clear()
            self._evicted_at = None


_LEDGER = ExternalContentLedger()


def get_external_content_ledger() -> ExternalContentLedger:
    return _LEDGER
