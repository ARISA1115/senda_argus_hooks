from __future__ import annotations

import inspect
import time
from typing import Any, Callable

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.runtime import emit_event, get_config
from senda_argus_hooks.instrumentors.base import BaseInstrumentor


class SendaArgusOpenAIAgentsProcessor:
    """Small trace-processor style object for OpenAI Agents SDK integrations.

    The OpenAI Agents SDK API may evolve, so this class intentionally exposes
    simple lifecycle methods that can be used directly in tests or wired into an
    SDK trace processor/exporter where available.
    """

    def on_trace_start(self, trace: Any) -> None:
        emit_event(
            "agent.run.started",
            source={"component": "integration", "sdk": "openai_agents", "operation": "trace.start"},
            data={"agent": {"framework": "openai_agents", "trace": _safe_value(trace)}},
            status="start",
        )

    def on_trace_end(self, trace: Any) -> None:
        emit_event(
            "agent.run.completed",
            source={"component": "integration", "sdk": "openai_agents", "operation": "trace.end"},
            data={"agent": {"framework": "openai_agents", "trace": _safe_value(trace)}},
            status="success",
        )

    def on_trace_error(self, trace: Any, error: BaseException) -> None:
        emit_event(
            "agent.run.failed",
            source={"component": "integration", "sdk": "openai_agents", "operation": "trace.error"},
            data={"agent": {"framework": "openai_agents", "trace": _safe_value(trace)}},
            status="error",
            error={"type": error.__class__.__name__, "message": str(error)},
        )

    def on_span_start(self, span: Any) -> None:
        emit_event(
            _span_event_type(span, suffix="started"),
            source={"component": "integration", "sdk": "openai_agents", "operation": "span.start"},
            data={"agent": {"framework": "openai_agents", "span": _safe_value(span)}},
            status="start",
        )

    def on_span_end(self, span: Any) -> None:
        emit_event(
            _span_event_type(span, suffix="completed"),
            source={"component": "integration", "sdk": "openai_agents", "operation": "span.end"},
            data={"agent": {"framework": "openai_agents", "span": _safe_value(span)}},
            status="success",
        )


class OpenAIAgentsInstrumentor(BaseInstrumentor):
    """Best-effort monkey patch for OpenAI Agents SDK Runner methods."""

    name = "openai_agents"

    def __init__(self) -> None:
        self._patches: list[tuple[Any, str, Callable[..., Any]]] = []

    def instrument(self) -> bool:
        candidates = []
        try:
            import agents  # type: ignore

            runner = getattr(agents, "Runner", None)
            if runner is not None:
                for method_name in ("run", "run_sync"):
                    original = getattr(runner, method_name, None)
                    if original is not None:
                        candidates.append((runner, method_name, original))
        except Exception:
            return False

        patched = False
        for cls, method_name, original in candidates:
            if hasattr(original, "__senda_patched__"):
                continue
            wrapped = self._wrap_async(original, method_name) if inspect.iscoroutinefunction(original) else self._wrap_sync(original, method_name)
            setattr(wrapped, "__senda_patched__", True)
            setattr(cls, method_name, wrapped)
            self._patches.append((cls, method_name, original))
            patched = True
        return patched

    def _wrap_sync(self, original: Callable[..., Any], operation: str) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            emit_event(
                "agent.run.started",
                source={"component": "instrumentor", "sdk": "openai_agents", "operation": operation},
                data={"agent": _run_payload(args, kwargs)},
                status="start",
            )
            try:
                response = original(*args, **kwargs)
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event(
                    "agent.run.completed",
                    source={"component": "instrumentor", "sdk": "openai_agents", "operation": operation},
                    data={"agent": _response_payload(response)},
                    status="success",
                    latency_ms=latency_ms,
                )
                return response
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event(
                    "agent.run.failed",
                    source={"component": "instrumentor", "sdk": "openai_agents", "operation": operation},
                    data={"agent": _run_payload(args, kwargs)},
                    status="error",
                    latency_ms=latency_ms,
                    error={"type": exc.__class__.__name__, "message": str(exc)},
                )
                raise

        return wrapper

    def _wrap_async(self, original: Callable[..., Any], operation: str) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            emit_event(
                "agent.run.started",
                source={"component": "instrumentor", "sdk": "openai_agents", "operation": operation},
                data={"agent": _run_payload(args, kwargs)},
                status="start",
            )
            try:
                response = await original(*args, **kwargs)
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event(
                    "agent.run.completed",
                    source={"component": "instrumentor", "sdk": "openai_agents", "operation": operation},
                    data={"agent": _response_payload(response)},
                    status="success",
                    latency_ms=latency_ms,
                )
                return response
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event(
                    "agent.run.failed",
                    source={"component": "instrumentor", "sdk": "openai_agents", "operation": operation},
                    data={"agent": _run_payload(args, kwargs)},
                    status="error",
                    latency_ms=latency_ms,
                    error={"type": exc.__class__.__name__, "message": str(exc)},
                )
                raise

        return wrapper

    def uninstrument(self) -> bool:
        for cls, method_name, original in self._patches:
            setattr(cls, method_name, original)
        self._patches = []
        return True


def _run_payload(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "framework": "openai_agents",
        "input_hash": sha256_value({"args": args, "kwargs": kwargs}),
    }
    if get_config().capture_arguments:
        payload["args"] = _safe_value(args)
        payload["kwargs"] = _safe_value(kwargs)
    return payload


def _response_payload(response: Any) -> dict[str, Any]:
    payload = {"framework": "openai_agents", "result_hash": sha256_value(_safe_value(response))}
    if get_config().capture_result:
        payload["result"] = _safe_value(response)
    return payload


def _span_event_type(span: Any, *, suffix: str) -> str:
    span_type = str(getattr(span, "type", None) or getattr(span, "span_type", None) or "step").lower()
    if "tool" in span_type:
        return "tool_call.requested" if suffix == "started" else "tool_call.completed"
    if "handoff" in span_type:
        return f"agent.handoff.{suffix}"
    if "generation" in span_type or "llm" in span_type:
        return "llm.request.started" if suffix == "started" else "llm.request"
    return f"agent.step.{suffix}"


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
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    return str(value)
