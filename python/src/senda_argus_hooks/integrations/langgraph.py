from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.runtime import emit_event, get_config


def stream_with_argus(graph: Any, input_data: Any = None, *args: Any, **kwargs: Any) -> Iterator[Any]:
    """Wrap a LangGraph-like stream() call and emit runtime events.

    This helper keeps LangGraph optional. It accepts any object exposing a
    stream(input, *args, **kwargs) method, which makes it testable with fake
    graph objects and usable with LangGraph compiled graphs.
    """
    start = time.perf_counter()
    graph_name = _graph_name(graph)
    emit_event(
        "agent.run.started",
        source={"component": "integration", "sdk": "langgraph", "operation": "stream"},
        data={"agent": {"framework": "langgraph", "graph": graph_name, "input_hash": sha256_value(input_data)}},
        status="start",
    )
    try:
        for chunk in graph.stream(input_data, *args, **kwargs):
            _emit_chunk(chunk, graph_name=graph_name, stream_mode=kwargs.get("stream_mode"))
            yield chunk
        latency_ms = int((time.perf_counter() - start) * 1000)
        emit_event(
            "agent.run.completed",
            source={"component": "integration", "sdk": "langgraph", "operation": "stream"},
            data={"agent": {"framework": "langgraph", "graph": graph_name}},
            status="success",
            latency_ms=latency_ms,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        emit_event(
            "agent.run.failed",
            source={"component": "integration", "sdk": "langgraph", "operation": "stream"},
            data={"agent": {"framework": "langgraph", "graph": graph_name}},
            status="error",
            latency_ms=latency_ms,
            error={"type": exc.__class__.__name__, "message": str(exc)},
        )
        raise


async def astream_with_argus(graph: Any, input_data: Any = None, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
    """Async variant of stream_with_argus for LangGraph-like astream() calls."""
    start = time.perf_counter()
    graph_name = _graph_name(graph)
    emit_event(
        "agent.run.started",
        source={"component": "integration", "sdk": "langgraph", "operation": "astream"},
        data={"agent": {"framework": "langgraph", "graph": graph_name, "input_hash": sha256_value(input_data)}},
        status="start",
    )
    try:
        async for chunk in graph.astream(input_data, *args, **kwargs):
            _emit_chunk(chunk, graph_name=graph_name, stream_mode=kwargs.get("stream_mode"))
            yield chunk
        latency_ms = int((time.perf_counter() - start) * 1000)
        emit_event(
            "agent.run.completed",
            source={"component": "integration", "sdk": "langgraph", "operation": "astream"},
            data={"agent": {"framework": "langgraph", "graph": graph_name}},
            status="success",
            latency_ms=latency_ms,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        emit_event(
            "agent.run.failed",
            source={"component": "integration", "sdk": "langgraph", "operation": "astream"},
            data={"agent": {"framework": "langgraph", "graph": graph_name}},
            status="error",
            latency_ms=latency_ms,
            error={"type": exc.__class__.__name__, "message": str(exc)},
        )
        raise


def _emit_chunk(chunk: Any, *, graph_name: str, stream_mode: Any) -> None:
    payload = _safe_value(chunk)
    data = {
        "agent": {
            "framework": "langgraph",
            "graph": graph_name,
            "stream_mode": stream_mode,
            "chunk_hash": sha256_value(payload),
        }
    }
    if get_config().capture_result:
        data["agent"]["chunk"] = payload
    emit_event(
        "agent.step.completed",
        source={"component": "integration", "sdk": "langgraph", "operation": "stream.chunk"},
        data=data,
        status="success",
    )


def _graph_name(graph: Any) -> str:
    return str(getattr(graph, "name", None) or getattr(graph, "__class__", type(graph)).__name__)


def _safe_value(value: Any) -> Any:
    for attr in ("model_dump", "dict"):
        if hasattr(value, attr):
            try:
                return getattr(value, attr)()
            except Exception:
                pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in value.items()}
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, str)):
        return [_safe_value(v) for v in value]
    return str(value)
