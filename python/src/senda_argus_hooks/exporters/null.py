from __future__ import annotations

from typing import Any

from .base import BaseExporter


class NullExporter(BaseExporter):
    def __init__(self, config: dict[str, Any] | None = None):
        pass

    def export(self, events: list[dict[str, Any]]) -> None:
        return None
