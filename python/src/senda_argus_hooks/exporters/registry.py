from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import BaseExporter

_EXPORTERS: dict[str, Callable[[dict[str, Any]], BaseExporter]] = {}


def register_exporter(name: str, factory: Callable[[dict[str, Any]], BaseExporter]) -> None:
    _EXPORTERS[name] = factory


def create_exporter(config: dict[str, Any]) -> BaseExporter:
    exporter_type = config.get("type")
    if not exporter_type:
        raise ValueError("Exporter config requires 'type'.")
    if exporter_type not in _EXPORTERS:
        raise ValueError(f"Unknown exporter type: {exporter_type}")
    return _EXPORTERS[exporter_type](config)


def available_exporters() -> list[str]:
    return sorted(_EXPORTERS)
