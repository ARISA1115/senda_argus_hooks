from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import BaseExporter


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

    def export(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        if self._run_id:
            events = [dict(ev, run_id=ev.get("run_id") or self._run_id) for ev in events]
        payload = json.dumps({"events": events}, ensure_ascii=False, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        req = urllib.request.Request(
            self._url, data=payload, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout):
                pass
        except (urllib.error.URLError, OSError):
            pass
