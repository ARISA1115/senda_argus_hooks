from __future__ import annotations

import time
from typing import Any, Callable

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.runtime import emit_event, get_config
from .base import BaseInstrumentor


class AnthropicInstrumentor(BaseInstrumentor):
    name = "anthropic"

    def __init__(self):
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        try:
            import anthropic
        except Exception:
            return False
        patched = False
        candidates = []
        try:
            candidates.append((anthropic.resources.messages.Messages, "create", "messages.create"))
        except Exception:
            pass
        for cls, method_name, op in candidates:
            current = getattr(cls, method_name, None)
            if current is None or hasattr(current, "__senda_patched__"):
                continue
            original = current
            wrapped = self._wrap(original, op)
            setattr(wrapped, "__senda_patched__", True)
            setattr(cls, method_name, wrapped)
            self._patches.append((cls, method_name, original))
            patched = True
        return patched

    def _wrap(self, original: Callable, operation: str) -> Callable:
        def wrapper(obj, *args, **kwargs):
            cfg = get_config()
            start = time.perf_counter()
            input_payload = {"args": args, "kwargs": kwargs} if cfg.capture_prompt else {"input_hash": sha256_value({"args": args, "kwargs": kwargs})}
            try:
                response = original(obj, *args, **kwargs)
                latency_ms = int((time.perf_counter() - start) * 1000)
                output_payload = _safe_response(response) if cfg.capture_response else {"response_hash": sha256_value(_safe_response(response))}
                emit_event(
                    "llm.request",
                    source={"component": "instrumentor", "sdk": "anthropic", "provider": "anthropic", "operation": operation},
                    data={"llm": {"provider": "anthropic", "operation": operation, "model": kwargs.get("model"), "input": input_payload, "output": output_payload}},
                    status="success",
                    latency_ms=latency_ms,
                )
                return response
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event("llm.error", source={"component": "instrumentor", "sdk": "anthropic", "provider": "anthropic", "operation": operation}, data={"llm": {"provider": "anthropic", "operation": operation, "model": kwargs.get("model"), "input": input_payload}}, status="error", latency_ms=latency_ms, error={"type": exc.__class__.__name__, "message": str(exc)})
                raise
        return wrapper

    def uninstrument(self) -> bool:
        for cls, method_name, original in self._patches:
            setattr(cls, method_name, original)
        self._patches = []
        return True


def _safe_response(response: Any) -> Any:
    for attr in ("model_dump", "dict"):
        if hasattr(response, attr):
            try:
                return getattr(response, attr)()
            except Exception:
                pass
    return str(response)
