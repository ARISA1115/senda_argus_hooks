from __future__ import annotations

import atexit
import json
import queue
import threading
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
_SHUTDOWN = object()


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
    送信エラーは無視してパイプラインを継続する (fire-and-forget)。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        endpoint = config.get("endpoint", "http://localhost:8000").rstrip("/")
        self._url = endpoint + "/v1/agent-runs/ingest"
        self._api_key: str = config.get("api_key", "")
        self._run_id: str | None = config.get("run_id")
        self._timeout: int = int(config.get("timeout", 10))
        self._queue: queue.Queue = queue.Queue(maxsize=_SEND_QUEUE_MAX)
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._atexit_registered = False

    def _send(self, payload: bytes, headers: dict[str, str]) -> None:
        req = urllib.request.Request(
            self._url, data=payload, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout):
                pass
        except (urllib.error.URLError, OSError):
            pass

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
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()
            if not self._atexit_registered:
                atexit.register(self.shutdown)
                self._atexit_registered = True

    def export(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        if self._run_id:
            events = [dict(ev, run_id=ev.get("run_id") or self._run_id) for ev in events]
        payload = json.dumps({"events": events}, ensure_ascii=False, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        # 送信をキューへ積み、単一ワーカーが FIFO 順にホスト経路の外で送る。キューが
        # 満杯なら捨てて呼び出し側を待たせない。ワーカーは 1 本に限定する。
        self._ensure_worker()
        try:
            self._queue.put_nowait((payload, headers))
        except queue.Full:
            pass

    def shutdown(self) -> None:
        # 終了時に積み残したバッチを送り切る。daemon ワーカーは通常終了で待たれないため、
        # EventBus.shutdown と atexit の双方からここを通して drain する。
        super().shutdown()
        worker = self._worker
        if worker is None or not worker.is_alive():
            return
        try:
            self._queue.put_nowait(_SHUTDOWN)
        except queue.Full:
            pass
        worker.join(timeout=_DRAIN_TIMEOUT)
