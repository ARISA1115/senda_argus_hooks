from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseExporter(ABC):
    @abstractmethod
    def export(self, events: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def shutdown(self) -> None:
        self.flush()
