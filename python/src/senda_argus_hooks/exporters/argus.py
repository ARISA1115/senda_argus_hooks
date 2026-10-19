from __future__ import annotations

import atexit
import contextlib
import datetime
import email.utils
import http.client
import json
import logging
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from senda_argus_hooks.core.device_identity import reserve_seq, sign_headers
from senda_argus_hooks.core.record_chain import SESSION_END_EVENT, RecordChain, seq_range

from .base import BaseExporter

# 送出は境界付きキューと単一のワーカースレッドで行う。バッチごとにスレッドを起こすと、
# 既定バッチサイズが 1 のため送信先が遅いとスレッドが無制限に積み上がってメモリや
# スレッド上限を圧迫し、start() が失敗すると同期送信に落ちてホスト経路を止める。
# 単一ワーカーで FIFO を保ち、shutdown / atexit で積み残しを送り切る。
_SEND_QUEUE_MAX = 1000
_DRAIN_TIMEOUT = 3.0
_STOP_GRACE = 0.5
_SHUTDOWN = object()
_DROP_WARN_INTERVAL = 60.0
# 捨てた記録の時間帯を報告する宛先と、持つ時間帯の数と、前の時間帯へ寄せる間隔。
_DISCARD_PATH = "/v1/hooks/discards"
_MAX_DISCARD_WINDOWS = 32
# 捨てた記録の番号の範囲を持つ数。受け取り側は、宣言された範囲の飛びを、宣言の無い飛びと区別して数える。
_MAX_DISCARD_RANGES = 64
_DISCARD_MERGE_SEC = 60.0
# 受け取り側が受ける時間帯の範囲。送る前に同じ範囲へ切り詰め、範囲外で報告ごと拒まれないようにする。
_DISCARD_MAX_SPAN = 7 * 86400.0
_DISCARD_MAX_AGE = 30 * 86400.0
# 受け取り側が 1 つの時間帯に受ける件数の上限。超えた時間帯は、他が受かる報告でも黙って捨てられる。
_DISCARD_MAX_COUNT = 100_000

# 受け取り側の停止 (計画メンテナンスと障害の復旧の作業) の間の再送。503 と、前段の CloudFront が
# 返す 502 と 504、IP の流量制限の 429、接続の失敗だけを送り直す。それ以外の失敗は送り直しても結果が
# 変わらない。送り直さない拒否で届かなかった記録は、破棄と同じ時間帯に数える。
# 送り直しは同じ本文を送るため、受け取り側は event_id で重複を除き、二重に記録しない。
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_MAX_ATTEMPTS = 5
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 30.0
# 計画メンテナンスの印 (x-argus-maintenance) の付いた 503 は回数で打ち切らず、Retry-After に従って
# 予定の最長 (7 日) まで送り直す。待つ間に積まれた分は送出キューの上限まで保ち、超えた分は破棄の件数に数える。
_MAINTENANCE_MAX_WAIT = 7 * 24 * 3600.0
_MAINTENANCE_MAX_DELAY = 900.0


def _retry_after_seconds(value: str) -> float | None:
    """Retry-After を待つ秒数にする。秒数と HTTP-date の両方を読み、読めなければ None を返す。"""
    text = value.strip()
    if text.isdigit():
        return float(int(text))
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        # HTTP-date は常に GMT で書かれる。-0000 のときは時差の無い値として返るため UTC とみなす。
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, when.timestamp() - time.time())

_logger = logging.getLogger("senda_argus_hooks.exporters.argus")

# 受け取り側が送り直しても受けないと返す符号。組織が停止している間の記録は、送り直しても同じ
# 403 になるため、引き取り型の収集でも取り直しの対象にしない。判定できない時の扱いが止めるの
# 符号、つまり Actions の不足、残量が分からない、閲覧専用も、送り直しても結果が変わらないため対象にしない。
# 保留の符号は 503 で返り、既存の送り直しの経路に乗る。
_TERMINAL_ERROR_CODES = frozenset(
    {"organization_suspended", "actions_exhausted", "quota_unknown", "organization_read_only"}
)
_REJECT_WARN_INTERVAL = 60.0


def _unjudged_reason_of_accepted(raw: bytes) -> str | None:
    """2xx の応答が判定を省いて受け付けたことを表すなら、その理由の符号を返す。"""
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, dict) or body.get("judged") is not False:
        return None
    reason = body.get("unjudged_reason")
    return reason if isinstance(reason, str) and reason else "unknown"


def _terminal_rejection_code(exc: urllib.error.HTTPError) -> str | None:
    """応答の本文が送り直しの対象外の符号を持てば、その符号を返す。読めなければ None。"""
    try:
        body = json.loads(exc.read(65536).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("error_code")
    if isinstance(code, str) and code in _TERMINAL_ERROR_CODES and body.get("retryable") is False:
        return code
    return None


def _read_body(response: Any) -> bytes | None:
    """応答の本文を上限つきで読む。読めなければ None を返す。読めなくても、送信の結果は変えない。

    分割転送の途中で切れた応答は、OSError でなく http.client.HTTPException の IncompleteRead になる。
    受けないと例外が送出のワーカーを止め、後の記録が送られず、持っている時間帯も戻されない。
    """
    try:
        return response.read(65536)
    except (http.client.HTTPException, OSError):
        return None


def _add_window(windows: list[list[float]], lo: float, hi: float, count: float) -> list[list[float]]:
    """時間帯を、開始の順を保って足す。前後 _DISCARD_MERGE_SEC の内で重なる時間帯は 1 つにまとめる。

    末尾へ足すだけだと、遅れた記録や時計の補正で古い範囲が来たとき並びが時刻の順でなくなり、上限での
    退避が時刻で離れた 2 つを寄せてしまう。
    """
    kept: list[list[float]] = []
    for w in windows:
        if lo <= w[1] + _DISCARD_MERGE_SEC and hi >= w[0] - _DISCARD_MERGE_SEC:
            lo, hi, count = min(lo, w[0]), max(hi, w[1]), count + w[2]
        else:
            kept.append(w)
    kept.append([lo, hi, count])
    kept.sort(key=lambda w: w[0])
    return kept


def _bound_windows(windows: list[list[float]]) -> list[list[float]]:
    """数が上限を超えたら、時刻の間隔が最も狭い隣り合う 2 つを 1 つにまとめる。件数は失わない。"""
    while len(windows) > _MAX_DISCARD_WINDOWS:
        i = min(range(len(windows) - 1), key=lambda k: windows[k + 1][0] - windows[k][1])
        a, b = windows[i], windows[i + 1]
        windows[i : i + 2] = [[min(a[0], b[0]), max(a[1], b[1]), a[2] + b[2]]]
    return windows


class ArgusExporter(BaseExporter):
    """Argus 検知エンジンの /v1/agent-runs/ingest エンドポイントへイベントを送信する。

    設定例:
        {
            "type": "argus",
            "endpoint": "http://localhost:8000",
            "api_key": "your-api-key",
            "run_id": "optional-fixed-run-id",
            "timeout": 10
        }

    endpoint + "/v1/agent-runs/ingest" に POST する。
    受け取り側が判定できない時は、止めるの符号 actions_exhausted、quota_unknown、
    organization_read_only を送り直さずに警告し、保留の 503 は送り直し、判定を省いた 202 は警告する。

    送信エラーでパイプラインを止めない。503、502、504 と接続の失敗だけは、retry_max_attempts 回
    (既定 5) まで間隔を伸ばして送り直し、届かなければ捨てた件数をログに出す。送り直しは同じ
    event_id のまま送るため、受け取り側は重複を除いて 1 件として記録する。

    指示ファイルの伝播の検知を効かせるには、api_key に収集用の鍵を設定する。受け取り側は、通常の
    テナントの鍵で届いた記録から書き込みと指示の証拠を採らない。収集用の鍵は発行時に並べた agent_id
    の記録に限って証拠を信頼させるため、この処理で動くエージェントの agent_id を並べて発行する。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        endpoint = config.get("endpoint", "http://localhost:8000").rstrip("/")
        self.endpoint = endpoint
        self._url = endpoint + "/v1/agent-runs/ingest"
        self._api_key: str = config.get("api_key", "")
        self._run_id: str | None = config.get("run_id")
        self._timeout: int = int(config.get("timeout", 10))
        # 行動の前の確認は行動を待たせるため、送出より短い時間で打ち切る。
        self.admission_timeout: float = max(0.1, float(config.get("admission_timeout", min(3.0, float(self._timeout)))))
        self._reject_warned: dict[str, float] = {}
        self._log_http: bool = bool(config.get("log_http", False))
        self._queue: queue.Queue = queue.Queue(maxsize=_SEND_QUEUE_MAX)
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._atexit_registered = False
        self._drop_lock = threading.Lock()
        self._dropped_events_count = 0
        self._last_drop_warn = 0.0
        self._retry_max_attempts: int = max(1, int(config.get("retry_max_attempts", _RETRY_MAX_ATTEMPTS)))
        self._retry_base_delay: float = max(0.0, float(config.get("retry_base_delay", _RETRY_BASE_DELAY)))
        self._retry_max_delay: float = max(0.0, float(config.get("retry_max_delay", _RETRY_MAX_DELAY)))
        self._maintenance_max_wait: float = max(0.0, float(config.get("retry_maintenance_max_wait", _MAINTENANCE_MAX_WAIT)))
        self._maintenance_max_delay: float = max(0.0, float(config.get("retry_maintenance_max_delay", _MAINTENANCE_MAX_DELAY)))
        self._stopping = threading.Event()
        self._unsent_events_count = 0
        self._last_unsent_warn = 0.0
        # 捨てた記録の時間帯 [始まり, 終わり, 件数]。送信が回復した後に Argus へ報告する。受け取り側は
        # この時間帯の中継にだけある記録を、hooks の記録の欠落ではなく取り込みの欠落として数える。
        self._discard_url = endpoint + _DISCARD_PATH
        self._discards: list[list[float]] = []
        self._seq_discards: list[list[Any]] = []
        # この送り先の記録の連鎖。送り先ごとに別の数え手を持つ。
        self._chain = RecordChain()
        # 番号を付けてからキューへ積むまでを 1 本の錠で囲む。別のスレッドが先に番号を受け取っても、
        # 積む順が番号の順と揺れると、受け取り側が記録を失っていないのに偽の飛びを見る。
        self._send_order_lock = threading.Lock()
        # 直近の _send_once が 2xx で受けられたか。再送しない拒否も (None, None) で返るので別に持つ。
        self._last_ingest_ok = False

    def _record_drop(self, count: int) -> None:
        """送出キュー満杯で捨てたイベント数を計数し、警告を一定間隔に間引いてログに残す。

        count は捨てたバッチに含まれるイベント数。バッチ単位でなくイベント単位で数えるため、
        バッチサイズが 1 を超えても欠落数を正しく表す。障害で連続 drop するとき export ごとに
        同期ログを書くとログが氾濫し呼び出し経路を塞ぐため、警告は間引いて非ブロッキング性を保つ。
        沈黙 drop で欠落を見失わない。
        """
        now = time.monotonic()
        with self._drop_lock:
            self._dropped_events_count += count
            total = self._dropped_events_count
            should_warn = (now - self._last_drop_warn) >= _DROP_WARN_INTERVAL
            if should_warn:
                self._last_drop_warn = now
        if should_warn:
            _logger.warning("Argus 送出キューが満杯のためイベントを破棄しました。累計 %d 件", total)

    def _note_discard(self, times: list[float], count: int) -> None:
        """捨てた記録の時刻の範囲を持つ。直前の時間帯に近ければ寄せ、数に上限を置く。"""
        lo, hi = (min(times), max(times)) if times else (time.time(), time.time())
        with self._drop_lock:
            self._discards = _bound_windows(_add_window(self._discards, lo, hi, float(count)))

    def _note_seq_ranges(self, ranges: list[tuple[str, int, int]]) -> None:
        """捨てた記録の番号の範囲を持つ。同じセッションで続く範囲は 1 つに寄せ、数に上限を置く。

        上限を超える分は、古い範囲から落とす。落とした範囲は宣言できず、受け取り側で宣言の無い飛びになる。
        """
        if not ranges:
            return
        with self._drop_lock:
            held = self._seq_discards
            for session, lo, hi in ranges:
                for item in held:
                    if item[0] == session and lo <= item[2] + 1 and hi >= item[1] - 1:
                        item[1], item[2] = min(item[1], lo), max(item[2], hi)
                        break
                else:
                    held.append([session, lo, hi])
            self._seq_discards = held[-_MAX_DISCARD_RANGES:]

    def _note_unsent(self, payload: bytes) -> None:
        """送れずに捨てた本文の時間帯と番号の範囲を持つ。"""
        self._note_discard(self._payload_times(payload), self._event_count(payload) or 1)
        self._note_seq_ranges(self._payload_seq_ranges(payload))

    @staticmethod
    def _payload_seq_ranges(payload: bytes) -> list[tuple[str, int, int]]:
        try:
            body = json.loads(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return []
        return seq_range(body.get("events") if isinstance(body, dict) else None)

    @staticmethod
    def _clip_windows(windows: list[list[float]], now: float) -> list[list[float]]:
        """受け取り側の範囲へ切り詰める。未来の終わりは今へ、長すぎる時間帯は終わりから 7 日へ寄せ、
        30 日より古い時間帯は捨てる。"""
        out: list[list[float]] = []
        for start, end, count in windows:
            end = min(end, now)
            start = min(max(start, end - _DISCARD_MAX_SPAN), end)
            if start < now - _DISCARD_MAX_AGE:
                continue
            # 件数が上限を超えると、受け取り側は時間帯ごと捨てる。上限へ切り詰めて時間帯を残す。
            out.append([start, end, min(max(count, 1.0), float(_DISCARD_MAX_COUNT))])
        return out

    def _report_discards(self, headers: dict[str, str]) -> None:
        """持っている時間帯を 1 回だけ報告する。届かなければ戻し、次の成功で送る。

        4xx は送り直しても同じ拒否になるので戻さない。
        """
        with self._drop_lock:
            windows, self._discards = self._discards, []
            ranges, self._seq_discards = self._seq_discards, []
        windows = self._clip_windows(windows, time.time())
        if not windows and not ranges:
            return
        body = json.dumps(
            {
                "windows": [{"from": w[0], "to": w[1], "count": int(w[2])} for w in windows],
                "ranges": [{"session": r[0], "from": r[1], "to": r[2]} for r in ranges],
            }
        ).encode("utf-8")
        # 渡されたヘッダーに記録の署名が残っていても、鍵が読めないときに報告へ付いたまま送らない。
        headers = {k: v for k, v in headers.items() if not k.lower().startswith("x-argus-device-")}
        # 本文が違うため、記録の送信に付けた署名は使えない。この本文で署名し直す。
        headers = {**headers, **sign_headers(body)}
        req = urllib.request.Request(self._discard_url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                status = int(getattr(response, "status", None) or response.getcode())
                raw = _read_body(response) or b""
            if 200 <= status < 300:
                self._log_rejected_windows(raw)
                return
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                _logger.warning("Argus が破棄の時間帯の報告を拒みました。送り直しません: status=%d", exc.code)
                return
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            pass
        with self._drop_lock:
            merged = self._discards
            for w in windows:
                merged = _add_window(merged, w[0], w[1], w[2])
            self._discards = _bound_windows(merged)
        self._note_seq_ranges([(r[0], r[1], r[2]) for r in ranges])

    def _read_accepted(self, response: Any, status: Any) -> bytes:
        """202 の本文を読む。判定を省いた受け付けかは本文にあるので、読めなければ見落とさず警告する。"""
        if int(status) != 202:
            return b""
        raw = _read_body(response)
        if raw is None:
            self._warn_rejection(
                "Argus の受け付けの本文を読めませんでした。判定を省いたかを確認できません: %s", "unreadable_accept"
            )
            return b""
        return raw

    @staticmethod
    def _log_rejected_windows(raw: bytes) -> None:
        """受け取り側が範囲外として受けなかった時間帯があれば、ログに残す。"""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        rejected = body.get("rejected") if isinstance(body, dict) else None
        if isinstance(rejected, int) and not isinstance(rejected, bool) and rejected > 0:
            _logger.warning("Argus が破棄の時間帯 %d 件を範囲外として受け付けませんでした", rejected)

    def _record_unsent(self, count: int, reason: str) -> None:
        """再送の上限まで送れずに捨てたイベント数を計数し、間引いてログに出す。"""
        now = time.monotonic()
        with self._drop_lock:
            self._unsent_events_count += count
            total = self._unsent_events_count
            should_warn = self._last_unsent_warn == 0.0 or (now - self._last_unsent_warn) >= _DROP_WARN_INTERVAL
            if should_warn:
                self._last_unsent_warn = now
        if should_warn:
            _logger.warning(
                "Argus へ %d 回送り直しても届かなかったためイベントを破棄しました。今回 %d 件、累計 %d 件: %s",
                self._retry_max_attempts,
                count,
                total,
                reason,
            )

    @property
    def api_key(self) -> str:
        return self._api_key

    def _warn_rejection(self, message: str, code: str) -> None:
        """受け取り側の拒否と判定の省略を、符号ごとに間引いて警告する。最初の 1 回は必ず出す。"""
        now = time.monotonic()
        with self._drop_lock:
            last = self._reject_warned.get(code)
            if last is not None and (now - last) < _REJECT_WARN_INTERVAL:
                return
            self._reject_warned[code] = now
        _logger.warning(message, code)

    def unsent_events(self) -> int:
        """再送の上限まで送れずに捨てたイベントの累計を返す。"""
        with self._drop_lock:
            return self._unsent_events_count

    def _retry_delay(self, attempt: int, retry_after: str | None, *, maintenance: bool = False) -> float:
        """attempt 回目の失敗の後に待つ秒数。Retry-After があればそれを上限の内で使う。"""
        delay = self._retry_base_delay * (2 ** (attempt - 1))
        if retry_after:
            parsed = _retry_after_seconds(retry_after)
            if parsed is not None:
                delay = parsed
        cap = self._maintenance_max_delay if maintenance else self._retry_max_delay
        return max(0.0, min(delay, cap))

    def dropped_events(self) -> int:
        """送出キュー満杯で捨てたバッチの累計を返す。"""
        with self._drop_lock:
            return self._dropped_events_count

    def _http_log(self, message: str) -> None:
        """Write transport-only diagnostics to stderr when explicitly enabled.

        The message never includes request headers, API keys, or event bodies.  This
        makes the output safe to surface in Agent Studio's Docker Logs panel while
        keeping normal production runtimes quiet unless SENDA_ARGUS_HTTP_LOG=true.
        """
        if not self._log_http:
            return
        print(f"[senda-argus-http] {message}", file=sys.stderr, flush=True)

    @staticmethod
    def _event_times(events: Any) -> list[float]:
        """記録の timestamp を epoch 秒で返す。読めない記録は数えない。"""
        out: list[float] = []
        for ev in events if isinstance(events, list) else []:
            raw = ev.get("timestamp") if isinstance(ev, dict) else None
            if not isinstance(raw, str):
                continue
            try:
                out.append(datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
            except ValueError:
                continue
        return out

    @classmethod
    def _payload_times(cls, payload: bytes) -> list[float]:
        try:
            body = json.loads(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return []
        return cls._event_times(body.get("events") if isinstance(body, dict) else None)

    @staticmethod
    def _event_count(payload: bytes) -> int | None:
        try:
            body = json.loads(payload.decode("utf-8"))
            events = body.get("events") if isinstance(body, dict) else None
            return len(events) if isinstance(events, list) else None
        except Exception:  # noqa: BLE001
            return None

    def _send(self, payload: bytes, headers: dict[str, str]) -> None:
        count = self._event_count(payload) if self._log_http else None
        suffix = f" events={count}" if count is not None else ""
        attempt = 0
        waited = 0.0
        # 再送は同じ通し番号で署名し直す。受け取り側は、同じ番号と本文の再送を 1 回だけ数える。
        seq = reserve_seq()
        while True:
            attempt += 1
            reason, retry_after = self._send_once(payload, headers, suffix, seq)
            if reason is None:
                # 報告するのは取り込みが 2xx で受けたときだけ。401 など再送しない拒否は回復ではなく、報告も
                # 同じ 4xx で拒まれて持っている時間帯が戻されずに消える。
                if self._last_ingest_ok:
                    self._report_discards(headers)
                else:
                    # 再送しない拒否 (401、403、400 など)。届かなかった記録を破棄と同じに数え、取り込みが
                    # 回復した後に時間帯を報告する。数えないと、受け取り側がその間の中継にだけある記録を
                    # hooks の欠落として警報にする。
                    self._note_unsent(payload)
                return
            maintenance = reason == "maintenance"
            exhausted = (
                waited >= self._maintenance_max_wait
                if maintenance
                else attempt >= self._retry_max_attempts
            )
            if exhausted or self._stopping.is_set():
                self._record_unsent(self._event_count(payload) or 1, reason)
                self._note_unsent(payload)
                return
            delay = self._retry_delay(attempt, retry_after, maintenance=maintenance)
            # 待ちが 0 秒でも予定の最長を数え尽くすよう、1 回を 1 秒以上と数える。
            waited += max(delay, 1.0)
            # 終了の要求が来たら待つのをやめ、送れなかった分として数える。
            if self._stopping.wait(delay):
                self._record_unsent(self._event_count(payload) or 1, reason)
                self._note_unsent(payload)
                return

    def _send_once(
        self,
        payload: bytes,
        headers: dict[str, str],
        suffix: str,
        seq: int | None = None,
    ) -> tuple[str | None, str | None]:
        """1 回送る。送り直す失敗なら (理由, Retry-After) を、それ以外は (None, None) を返す。

        (None, None) は成功と、再送しない拒否の両方を含む。成功かどうかは self._last_ingest_ok が持つ。
        """
        self._last_ingest_ok = False
        # 再送のたびに、新しい時刻と通し番号で署名し直す。鍵が無い端末は、署名を付けずに送る。
        headers = {**headers, **sign_headers(payload, seq=seq)}
        req = urllib.request.Request(
            self._url, data=payload, method="POST", headers=headers
        )
        self._http_log(f"POST {self._url}{suffix} bytes={len(payload)}")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                raw = self._read_accepted(response, status)
            self._http_log(f"POST completed status={status} url={self._url}{suffix}")
            self._check_unjudged_accept(raw)
            self._last_ingest_ok = 200 <= int(status) < 300
            return None, None
        except urllib.error.HTTPError as exc:
            self._http_log(f"POST failed status={exc.code} url={self._url}{suffix} error={exc.reason}")
            # 組織の停止と、判定できない時の止めるの符号は 403 で返る。送り直しの対象の状態と重ならないが、
            # 順に判定する。
            code = _terminal_rejection_code(exc) if exc.code not in _RETRY_STATUSES else None
            if code is not None:
                self._warn_rejection("Argus が受信を拒みました。送り直しません: %s", code)
                return None, None
            if exc.code in _RETRY_STATUSES:
                retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
                marked = exc.headers is not None and bool((exc.headers.get("x-argus-maintenance") or "").strip())
                return ("maintenance" if exc.code == 503 and marked else f"status={exc.code}"), retry_after
            return None, None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            self._http_log(f"POST failed url={self._url}{suffix} error={reason}")
            return f"connection={type(reason).__name__}", None

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SHUTDOWN:
                    return
                payload, headers = item
                self._send(payload, headers)
            finally:
                self._queue.task_done()

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stopping.clear()
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()
            if not self._atexit_registered:
                atexit.register(self.shutdown)
                self._atexit_registered = True

    def _request(self, events: list[dict[str, Any]], *, chained: bool = False) -> tuple[bytes, dict[str, str]]:
        if self._run_id:
            events = [dict(ev, run_id=ev.get("run_id") or self._run_id) for ev in events]
        if chained:
            # 通し番号は、記録を捉えた時点で付ける。送信の待ち行列へ積む前に付けるため、溢れて捨てた記録も番号を持つ。
            events = self._chain.stamp(events)
        payload = json.dumps({"events": events}, ensure_ascii=False, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return payload, headers

    def send_sync(self, events: list[dict[str, Any]]) -> bool:
        """待って送り、受け取り側が 2xx を返したかを返す。

        引き取り型の収集は、届いたことを確かめてから取得位置を進める。キューへ積むだけの export では
        届かなかった記録を取り直せない。受け取り側は event_id で重複を除くため、送り直しても二重に
        数えない。

        受け取り側が送り直しの対象外の符号 (組織の停止中) を返したときは、送り直しても同じ拒否になる
        ため True を返して取得位置を進める。停止の間の記録は取り直さない。
        """
        if not events:
            return True
        payload, headers = self._request(events)
        signed = {**headers, **sign_headers(payload)}
        req = urllib.request.Request(self._url, data=payload, method="POST", headers=signed)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                raw = self._read_accepted(response, status)
        except urllib.error.HTTPError as exc:
            self._http_log(f"POST failed status={exc.code} url={self._url}")
            # 保留の符号は 503 で返り、False を返して取り直しの対象に残す。
            code = _terminal_rejection_code(exc) if exc.code not in _RETRY_STATUSES else None
            if code is not None:
                self._warn_rejection("Argus が受信を拒みました。送り直しません: %s", code)
                return True
            return False
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            self._http_log(f"POST failed url={self._url} error={getattr(exc, 'reason', exc)}")
            return False
        self._check_unjudged_accept(raw)
        ok = 200 <= int(status) < 300
        if ok:
            self._report_discards(headers)
        return ok

    def _check_unjudged_accept(self, raw: bytes) -> None:
        """受け取り側が判定を省いて受け付けたときに、理由を警告する。"""
        if not raw:
            return
        reason = _unjudged_reason_of_accepted(raw)
        if reason is not None:
            self._warn_rejection("Argus が判定を省いて記録を受け付けました: %s", reason)

    def export(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        # 番号を付けてからキューへ積むまでを 1 本の錠で囲み、番号の順と積む順を揺らさない。
        with self._send_order_lock:
            payload, headers = self._request(events, chained=True)
            # 送信をキューへ積み、単一ワーカーが FIFO 順にホスト経路の外で送る。キューが
            # 満杯なら捨てて呼び出し側を待たせない。ワーカーは 1 本に限定する。
            self._ensure_worker()
            try:
                self._queue.put_nowait((payload, headers))
            except queue.Full:
                self._record_drop(len(events))
                self._note_discard(self._event_times(events), len(events))
                self._note_seq_ranges(self._payload_seq_ranges(payload))

    def chain_position(self) -> tuple[str, int]:
        """(セッション, 直近の番号)。生存の通知に載せる。"""
        return self._chain.position()

    def _enqueue_session_end(self, worker: threading.Thread | None) -> None:
        """セッションの終了を、署名つきの通知として 1 回だけ送る。

        終了の通知が無く途切れたセッションを、受け取り側は未終了として数える。末尾の記録を消して新しいセッションを
        始める手口を、その数で見つける。送出のワーカーが動いていない (記録を 1 件も送っていない) ときは、送るものが無い。
        キューが満杯で積めなかった通知は、番号の範囲を、捨てた記録として宣言する。
        """
        if worker is None or not worker.is_alive():
            return
        try:
            from senda_argus_hooks.core.event import new_event
            from senda_argus_hooks.core.runtime import effective_agent_id, get_config

            cfg = get_config()
            agent = effective_agent_id()

            def build(session: str, last_seq: int) -> dict[str, Any]:
                return new_event(
                    project=cfg.project,
                    environment=cfg.environment,
                    event_type=SESSION_END_EVENT,
                    source={"component": "exporter", "sdk": "senda_argus_hooks"},
                    data={"last_seq": last_seq},
                    status="success",
                    run_id=cfg.run_id or f"run_session_end_{session}",
                    agent_id=agent,
                ).to_dict()

            stamped = self._chain.stamp_end(build)
            if stamped is None:
                return
            payload, headers = self._request([stamped], chained=False)
        except Exception:  # noqa: BLE001 - 終了の通知で、終了の処理を止めない
            _logger.debug("セッションの終了の通知を作れませんでした", exc_info=True)
            return
        try:
            self._queue.put_nowait((payload, headers))
        except queue.Full:
            self._note_seq_ranges(self._payload_seq_ranges(payload))

    def shutdown(self) -> None:
        # 終了時に積み残したバッチを送り切る。daemon ワーカーは通常終了で待たれないため、
        # EventBus.shutdown と atexit の双方からここを通して drain する。
        super().shutdown()
        worker = self._worker
        self._enqueue_session_end(worker)
        if worker is None or not worker.is_alive():
            return
        # ワーカーは FIFO を引き取りながら draining 中なので、満杯でも空きはすぐ空く。put_nowait だと
        # 直前の終了通知がちょうど最後の 1 枠を使った場合に即座に Full になり、符を積めずワーカーが
        # get() で永久に待ち続ける。空きを待って積み、確実に届ける。
        with contextlib.suppress(queue.Full):
            self._queue.put(_SHUTDOWN, timeout=_DRAIN_TIMEOUT)
        worker.join(timeout=_DRAIN_TIMEOUT)
        # 待つ時間の内に送り切れなければ、送り直しの待ちを打ち切る。打ち切った分は送れなかった件数に
        # 数えてログに出す。
        self._stopping.set()
        worker.join(timeout=_STOP_GRACE)
