"""hook が送る記録の通し番号と連鎖。

記録に、セッションの識別子、1 から始まる通し番号、前の記録の要約 (digest) を付ける。受け取り側は、番号の飛びと
連鎖の不一致を欠落として記録する。中継を通らない呼び出しの記録が途中で消えても、これで気付ける。

- **番号は記録を捉えた時点で付ける。** 送信の時に付けると、送出キューから溢れて捨てた記録が番号を持たず、飛びにならずに
  欠落が見えなくなる。捨てた記録は、番号の範囲として、捨てた件数の報告に載せる。
- **数え手はプロセスの中に 1 つ。** 全ての計装とスレッドが同じ数え手を使う。fork した子は、別のセッションにする。
- 連鎖は監査の連鎖とは別。式は、hook_chain に session と seq と prev を入れた記録を、キーを並べ替え、区切りを
  (",", ":") にし、ensure_ascii=False で直列化した UTF-8 の SHA-256。送り手が申告した時刻は使わない。
- 標準ライブラリだけで動く。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from typing import Any, Optional

CHAIN_FIELD = "hook_chain"
SESSION_END_EVENT = "hook_session_end"


def digest_of(event: dict[str, Any]) -> str:
    """hook_chain を含む記録の要約。受け取り側が同じ式で計算し直す。"""
    raw = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RecordChain:
    __slots__ = ("_lock", "_pid", "_session", "_seq", "_prev", "_ended")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._session = uuid.uuid4().hex
        self._seq = 0
        self._prev = ""
        self._ended = False

    def _renew_if_forked(self) -> None:
        # fork した子は親の続きを書かない。別のプロセスは別のセッションにする。
        if os.getpid() != self._pid:
            self._pid = os.getpid()
            self._session = uuid.uuid4().hex
            self._seq = 0
            self._prev = ""
            self._ended = False

    def stamp(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """記録に通し番号と前の要約を付けた写しを返す。順序は渡された順。

        直列化して読み直した写しに付ける。送る本文を受け取り側が読み直した値と同じにし、要約を一致させる。
        """
        out: list[dict[str, Any]] = []
        with self._lock:
            self._renew_if_forked()
            for event in events:
                canon = json.loads(json.dumps(event, ensure_ascii=False, default=str))
                self._seq += 1
                canon[CHAIN_FIELD] = {"session": self._session, "seq": self._seq, "prev": self._prev}
                self._prev = digest_of(canon)
                out.append(canon)
        return out

    def position(self) -> tuple[str, int]:
        """(セッション, 直近に番号を付けた記録の番号)。生存の通知に載せる。"""
        with self._lock:
            self._renew_if_forked()
            return self._session, self._seq

    def take_end(self) -> Optional[tuple[str, int]]:
        """セッションの終了を 1 回だけ知らせるための (セッション, 最後の番号)。2 回目は None。"""
        with self._lock:
            self._renew_if_forked()
            if self._ended:
                return None
            self._ended = True
            return self._session, self._seq


_chain = RecordChain()


def chain() -> RecordChain:
    return _chain


def reset_for_tests() -> None:
    global _chain
    _chain = RecordChain()


def seq_range(events: Any) -> list[tuple[str, int, int]]:
    """記録の並びから、セッションごとの番号の範囲 (セッション, 始まり, 終わり) を返す。連鎖を持たない記録は数えない。"""
    spans: dict[str, list[int]] = {}
    for event in events if isinstance(events, list) else []:
        meta = event.get(CHAIN_FIELD) if isinstance(event, dict) else None
        if not isinstance(meta, dict):
            continue
        session, seq = meta.get("session"), meta.get("seq")
        if not isinstance(session, str) or isinstance(seq, bool) or not isinstance(seq, int):
            continue
        lo_hi = spans.setdefault(session, [seq, seq])
        lo_hi[0], lo_hi[1] = min(lo_hi[0], seq), max(lo_hi[1], seq)
    return [(s, v[0], v[1]) for s, v in spans.items()]
