from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .base import BaseExporter


_FLAT_KEYS = [
    "schema_version",
    "event_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "timestamp",
    "project",
    "environment",
    "event_type",
    "status",
    "latency_ms",
]


def _compact_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def flatten_event(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") or {}
    source = event.get("source") or {}
    actor = event.get("actor") or {}
    mcp = data.get("mcp") or {}
    llm = data.get("llm") or {}
    row = {key: event.get(key) for key in _FLAT_KEYS}
    row.update(
        {
            "provider": llm.get("provider") or source.get("provider"),
            "operation": source.get("operation") or data.get("operation"),
            "model": llm.get("model") or data.get("model"),
            "agent_id": actor.get("agent_id") or data.get("agent_id"),
            "user_id": actor.get("user_id") or data.get("user_id"),
            "mcp_server": mcp.get("server"),
            "mcp_tool": mcp.get("tool"),
            "source_json": _compact_json(source),
            "actor_json": _compact_json(actor),
            "data_json": _compact_json(data),
            "security_json": _compact_json(event.get("security")),
            "error_json": _compact_json(event.get("error")),
            "raw_json": _compact_json(event),
        }
    )
    return row


class ParquetExporter(BaseExporter):
    """Writes rotated parquet files.

    Parquet is not a simple append format, so this exporter buffers events and
    writes new part files such as events-20260625-120000-000001.parquet.
    """

    def __init__(self, config: dict[str, Any]):
        self.dir = Path(config.get("dir", "./senda-parquet"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flush_size = int(config.get("flush_size", 1000))
        self.compression = config.get("compression", "zstd")
        self.buffer: list[dict[str, Any]] = []
        self.lock = Lock()
        self.part = 0

    def export(self, events: list[dict[str, Any]]) -> None:
        with self.lock:
            self.buffer.extend(events)
            if len(self.buffer) >= self.flush_size:
                self._flush_locked()

    def flush(self) -> None:
        with self.lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self.buffer:
            return
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("Parquet exporter requires pyarrow. Install with: pip install senda-argus-hooks[parquet]") from exc
        rows = [flatten_event(event) for event in self.buffer]
        self.buffer = []
        self.part += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = self.dir / f"events-{stamp}-{self.part:06d}.parquet"
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, path, compression=self.compression)
