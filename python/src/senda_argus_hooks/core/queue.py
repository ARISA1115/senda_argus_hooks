from __future__ import annotations

import atexit
import contextlib
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
        with contextlib.suppress(Exception), self.lock:
            self.buffer.append(event)
            if len(self.buffer) >= self.batch_size:
                self._flush_locked()

    def flush(self) -> None:
        with contextlib.suppress(Exception), self.lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self.buffer:
            return
        events = self.buffer
        self.buffer = []
        for exporter in self.exporters:
            # Do not break user workloads due to audit exporter errors.
            with contextlib.suppress(Exception):
                exporter.export(events)

    def shutdown(self) -> None:
        self.flush()
        for exporter in self.exporters:
            with contextlib.suppress(Exception):
                exporter.shutdown()
