from __future__ import annotations

import time
from typing import Any, Callable

from senda_argus_hooks.core.runtime import emit_event, get_config
from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.response_meta import extract_response_model as _extract_response_model
from .base import BaseInstrumentor


class OpenAIInstrumentor(BaseInstrumentor):
    name = "openai"

    def __init__(self):
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        try:
            import openai
        except Exception:
            return False
        patched = False
        candidates = []
        try:
            candidates.append((openai.resources.chat.completions.Completions, "create", "chat.completions.create"))
        except Exception:
            pass
        try:
            candidates.append((openai.resources.responses.Responses, "create", "responses.create"))
        except Exception:
            pass
        try:
            candidates.append((openai.resources.embeddings.Embeddings, "create", "embeddings.create"))
        except Exception:
            pass
        for cls, method_name, op in candidates:
            if hasattr(getattr(cls, method_name, None), "__senda_patched__"):
                continue
            original = getattr(cls, method_name)
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
                llm_data: dict[str, Any] = {
                    "provider": "openai",
                    "operation": operation,
                    "model": kwargs.get("model"),
                    "input": input_payload,
                    "output": output_payload,
                }
                if "messages" in kwargs:
                    llm_data["messages_hash"] = sha256_value(kwargs.get("messages") or [])
                usage = _extract_usage(response)
                if usage:
                    llm_data["usage"] = usage
                response_model = _extract_response_model(response)
                if response_model:
                    llm_data["response_model"] = response_model
                emit_event(
                    "llm.request",
                    source={"component": "instrumentor", "sdk": "openai", "provider": "openai", "operation": operation},
                    data={"llm": llm_data},
                    status="success",
                    latency_ms=latency_ms,
                )
                offered = _offered_tool_names(kwargs)
                selected = _selected_tool_name(response)
                if offered and selected:
                    emit_event(
                        "agent.decision",
                        source={"component": "instrumentor", "sdk": "openai", "provider": "openai", "operation": operation},
                        data={
                            "alternatives": [{"name": name} for name in offered],
                            "selected_tool": selected,
                        },
                        status="success",
                        latency_ms=latency_ms,
                    )
                return response
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event(
                    "llm.error",
                    source={"component": "instrumentor", "sdk": "openai", "provider": "openai", "operation": operation},
                    data={"llm": {"provider": "openai", "operation": operation, "model": kwargs.get("model"), "input": input_payload}},
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

def _offered_tool_names(kwargs) -> list[str]:
    """LLM 呼び出しに渡された提示ツール集合の名前を取り出す。

    chat.completions は tools=[{"type":"function","function":{"name":...}}]、
    responses API は tools=[{"type":"function","name":...}] の形をとる。
    どちらの形からも関数名を拾う。
    """
    tools = kwargs.get("tools")
    names: list[str] = []
    if isinstance(tools, (list, tuple)):
        for tool in tools:
            name = ""
            if isinstance(tool, dict):
                fn = tool.get("function")
                if isinstance(fn, dict):
                    name = str(fn.get("name") or "")
                if not name:
                    name = str(tool.get("name") or "")
            if name:
                names.append(name)
    return names


def _selected_tool_name(response: Any) -> str:
    """LLM 応答でモデルが選択したツール名を取り出す。無ければ空文字。

    chat.completions は choices[].message.tool_calls[].function.name、
    responses API は output[].name (type=function_call) を参照する。
    """
    resp = _safe_response(response)
    if not isinstance(resp, dict):
        return ""
    choices = resp.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if isinstance(call, dict):
                        fn = call.get("function")
                        name = str(fn.get("name") or "") if isinstance(fn, dict) else ""
                        if name:
                            return name
    output = resp.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") in ("function_call", "tool_call"):
                name = str(item.get("name") or "")
                if name:
                    return name
    return ""


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

    input_tokens = _get("prompt_tokens")
    output_tokens = _get("completion_tokens")
    result: dict[str, int] = {}
    if input_tokens is not None:
        result["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        result["output_tokens"] = int(output_tokens)
    return result or None
