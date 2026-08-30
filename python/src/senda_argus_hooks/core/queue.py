from __future__ import annotations

import atexit
from threading import Lock
from typing import Any

from senda_argus_hooks.exporters.base import BaseExporter


class EventBus:
    def __init__(self, exporters: list[BaseExporter] | None = None, batch_size: int = 1):
        self.exporters = exporters or []
        self.batch_size = batch_size
        self.buffer: list[dict[str, Any]] = []
        self.lock = Lock()
        atexit.register(self.shutdown)

    def emit(self, event: dict[str, Any]) -> None:
        # Hook SDK must never break the host application.
        try:
            with self.lock:
                self.buffer.append(event)
                if len(self.buffer) >= self.batch_size:
                    self._flush_locked()
        except Exception:
            pass

    def flush(self) -> None:
        try:
            with self.lock:
                self._flush_locked()
        except Exception:
            pass

    def _flush_locked(self) -> None:
        if not self.buffer:
            return
        events = self.buffer
        self.buffer = []
        for exporter in self.exporters:
            try:
                exporter.export(events)
            except Exception:
                # Do not break user workloads due to audit exporter errors.
                pass

    def shutdown(self) -> None:
        self.flush()
        for exporter in self.exporters:
            try:
                exporter.shutdown()
            except Exception:
                pass
