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
            input_payload = _input_payload(args, kwargs, cfg.capture_prompt, cfg.capture_hash)
            try:
                response = original(obj, *args, **kwargs)
                latency_ms = int((time.perf_counter() - start) * 1000)
                output_payload = _safe_response(response) if cfg.capture_response else {"response_hash": sha256_value(_safe_response(response))}
                messages_hash = sha256_value(kwargs.get("messages") or [])
                llm_data: dict[str, Any] = {"provider": "anthropic", "operation": operation, "model": kwargs.get("model"), "messages_hash": messages_hash, "input": input_payload, "output": output_payload}
                usage = _extract_usage(response)
                if usage:
                    llm_data["usage"] = usage
                response_model = _extract_response_model(response)
                if response_model:
                    llm_data["response_model"] = response_model
                emit_event(
                    "llm.request",
                    source={"component": "instrumentor", "sdk": "anthropic", "provider": "anthropic", "operation": operation},
                    data={"llm": llm_data},
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



def _input_payload(args, kwargs, capture: bool, capture_hash: bool = True) -> dict[str, Any]:
    payload = {"args": args, "kwargs": kwargs}
    metadata = _llm_input_hash_metadata(args, kwargs) if capture_hash else {}
    if capture:
        return {**payload, **metadata}
    return {"input_hash": sha256_value(payload), **metadata}


def _llm_input_hash_metadata(args, kwargs) -> dict[str, Any]:
    messages = _extract_messages(args, kwargs)
    metadata: dict[str, Any] = {
        "args_hash": sha256_value(args),
        "kwargs_hash": sha256_value(kwargs),
    }
    if messages is not None:
        message_hashes = []
        for index, message in enumerate(messages):
            if isinstance(message, dict):
                role = message.get("role")
                content = message.get("content")
            else:
                role = getattr(message, "role", None)
                content = getattr(message, "content", None)
            item = {
                "index": index,
                "role": role,
                "content_hash": sha256_value(content),
            }
            if isinstance(content, str):
                item["content_length"] = len(content)
            message_hashes.append({k: v for k, v in item.items() if v is not None})
        metadata.update(
            {
                "messages_count": len(messages),
                "messages_hash": sha256_value(messages),
                "message_content_hashes": message_hashes,
            }
        )
    return metadata


def _extract_messages(args, kwargs) -> list[Any] | None:
    messages = kwargs.get("messages")
    if messages is None and args:
        first = args[0]
        if isinstance(first, list):
            messages = first
        elif isinstance(first, dict):
            messages = first.get("messages") or first.get("request_body", {}).get("messages")
    if messages is None:
        request_body = kwargs.get("request_body")
        if isinstance(request_body, dict):
            messages = request_body.get("messages")
    if isinstance(messages, tuple):
        messages = list(messages)
    return messages if isinstance(messages, list) else None

def _safe_response(response: Any) -> Any:
    for attr in ("model_dump", "dict"):
        if hasattr(response, attr):
            try:
                return getattr(response, attr)()
            except Exception:
                pass
    return str(response)


def _extract_usage(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None

    def _get(name: str) -> Any:
        return getattr(usage, name, None) if not isinstance(usage, dict) else usage.get(name)

    input_tokens = _get("input_tokens")
    output_tokens = _get("output_tokens")
    result: dict[str, int] = {}
    if input_tokens is not None:
        result["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        result["output_tokens"] = int(output_tokens)
    return result or None


def _extract_response_model(response: Any) -> str | None:
    """レスポンスが自己申告する実モデル識別子を抽出する。

    リクエストで指定した model との不一致を Argus 側の ModelSwapRule が
    検知できるよう、response_model として別フィールドで送出する。
    """
    model = getattr(response, "model", None)
    if model is None and isinstance(response, dict):
        model = response.get("model")
    if isinstance(model, str) and model.strip():
        return model
    return None
