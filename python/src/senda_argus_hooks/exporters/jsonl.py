from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from .base import BaseExporter


class JsonlExporter(BaseExporter):
    def __init__(self, config: dict[str, Any]):
        self.path = Path(config.get("path", "./senda-events.jsonl"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()

    def export(self, events: list[dict[str, Any]]) -> None:
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                for event in events:
                    f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
