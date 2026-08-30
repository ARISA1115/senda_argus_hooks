from __future__ import annotations

import json
from typing import Any

from .base import BaseExporter


class StdoutExporter(BaseExporter):
    def __init__(self, config: dict[str, Any] | None = None):
        self.pretty = bool((config or {}).get("pretty", False))

    def export(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            if self.pretty:
                print(json.dumps(event, ensure_ascii=False, indent=2, default=str))
            else:
                print(json.dumps(event, ensure_ascii=False, default=str))
