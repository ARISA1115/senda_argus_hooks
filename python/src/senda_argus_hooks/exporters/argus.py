from __future__ import annotations

import atexit
import contextlib
import datetime
import email.utils
import json
import logging
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

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

# 受け取り側の停止 (計画メンテナンスと障害の復旧の作業) の間の再送。503 と、前段の CloudFront が
# 返す 502 と 504、接続の失敗だけを送り直す。それ以外の失敗は送り直しても結果が変わらない。
# 送り直しは同じ本文を送るため、受け取り側は event_id で重複を除き、二重に記録しない。
_RETRY_STATUSES = frozenset({502, 503, 504})
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
        while True:
            attempt += 1
            reason, retry_after = self._send_once(payload, headers, suffix)
            if reason is None:
                return
            maintenance = reason == "maintenance"
            exhausted = (
                waited >= self._maintenance_max_wait
                if maintenance
                else attempt >= self._retry_max_attempts
            )
            if exhausted or self._stopping.is_set():
                self._record_unsent(self._event_count(payload) or 1, reason)
                return
            delay = self._retry_delay(attempt, retry_after, maintenance=maintenance)
            # 待ちが 0 秒でも予定の最長を数え尽くすよう、1 回を 1 秒以上と数える。
            waited += max(delay, 1.0)
            # 終了の要求が来たら待つのをやめ、送れなかった分として数える。
            if self._stopping.wait(delay):
                self._record_unsent(self._event_count(payload) or 1, reason)
                return

    def _send_once(
        self, payload: bytes, headers: dict[str, str], suffix: str
    ) -> tuple[str | None, str | None]:
        """1 回送る。送り直す失敗なら (理由, Retry-After) を、それ以外は (None, None) を返す。"""
        req = urllib.request.Request(
            self._url, data=payload, method="POST", headers=headers
        )
        self._http_log(f"POST {self._url}{suffix} bytes={len(payload)}")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                raw = response.read(65536) if int(status) == 202 else b""
            self._http_log(f"POST completed status={status} url={self._url}{suffix}")
            self._check_unjudged_accept(raw)
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
        except (urllib.error.URLError, OSError) as exc:
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

    def _request(self, events: list[dict[str, Any]]) -> tuple[bytes, dict[str, str]]:
        if self._run_id:
            events = [dict(ev, run_id=ev.get("run_id") or self._run_id) for ev in events]
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
        req = urllib.request.Request(self._url, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                raw = response.read(65536) if int(status) == 202 else b""
        except urllib.error.HTTPError as exc:
            self._http_log(f"POST failed status={exc.code} url={self._url}")
            # 保留の符号は 503 で返り、False を返して取り直しの対象に残す。
            code = _terminal_rejection_code(exc) if exc.code not in _RETRY_STATUSES else None
            if code is not None:
                self._warn_rejection("Argus が受信を拒みました。送り直しません: %s", code)
                return True
            return False
        except (urllib.error.URLError, OSError) as exc:
            self._http_log(f"POST failed url={self._url} error={getattr(exc, 'reason', exc)}")
            return False
        self._check_unjudged_accept(raw)
        return 200 <= int(status) < 300

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
        payload, headers = self._request(events)
        # 送信をキューへ積み、単一ワーカーが FIFO 順にホスト経路の外で送る。キューが
        # 満杯なら捨てて呼び出し側を待たせない。ワーカーは 1 本に限定する。
        self._ensure_worker()
        try:
            self._queue.put_nowait((payload, headers))
        except queue.Full:
            self._record_drop(len(events))

    def shutdown(self) -> None:
        # 終了時に積み残したバッチを送り切る。daemon ワーカーは通常終了で待たれないため、
        # EventBus.shutdown と atexit の双方からここを通して drain する。
        super().shutdown()
        worker = self._worker
        if worker is None or not worker.is_alive():
            return
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(_SHUTDOWN)
        worker.join(timeout=_DRAIN_TIMEOUT)
        # 待つ時間の内に送り切れなければ、送り直しの待ちを打ち切る。打ち切った分は送れなかった件数に
        # 数えてログに出す。
        self._stopping.set()
        worker.join(timeout=_STOP_GRACE)
