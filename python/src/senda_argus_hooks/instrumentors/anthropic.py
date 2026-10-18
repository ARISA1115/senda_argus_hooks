from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.instruction_files import (
    collect_instruction_sources,
    system_prompt_line_digests,
    system_prompt_pair_digests,
    system_prompt_semantic_digests,
)
from senda_argus_hooks.core.mcp_tools import offered_alternatives
from senda_argus_hooks.core.response_meta import (
    extract_response_model as _extract_response_model,
)
from senda_argus_hooks.core.runtime import emit_event, get_config

from .anthropic_stream import (
    USAGE_FIELDS,
    StreamedMessage as _StreamedMessage,
    UsageStream as _UsageStream,
    UsageStreamManager as _UsageStreamManager,
)
from .base import BaseInstrumentor, audit_guard

# 計装している呼び出し。Argus の突合の画面が、数えている範囲として出す。Batch の API は、結果の取得が
# 課金の日と別の日になり、日ごとの突合に使えないため、計装していない。
INSTRUMENTED_CALLS = (
    "messages.create",
    "messages.create(async)",
    "messages.create(stream=True)",
    "messages.stream",
    "messages.stream(async)",
)


class AnthropicInstrumentor(BaseInstrumentor):
    name = "anthropic"

    def __init__(self):
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        try:
            import anthropic
        except Exception:  # noqa: BLE001 - 任意の SDK の import 失敗は種類を問わず未導入として扱う
            return False
        patched = False
        candidates: list[tuple[Any, str, str, str]] = []
        messages_module = getattr(getattr(anthropic, "resources", None), "messages", None)
        for attr, kind_create in (("Messages", "sync"), ("AsyncMessages", "async")):
            cls = getattr(messages_module, attr, None)
            if cls is None:
                continue
            candidates.append((cls, "create", "messages.create", kind_create))
            if hasattr(cls, "stream"):
                candidates.append((cls, "stream", "messages.stream", "stream"))
        for cls, method_name, op, kind in candidates:
            current = getattr(cls, method_name, None)
            if current is None or hasattr(current, "__senda_patched__"):
                continue
            original = current
            if kind == "async":
                wrapped = self._wrap_async(original, op)
            elif kind == "stream":
                wrapped = self._wrap_stream(original, op)
            else:
                wrapped = self._wrap(original, op)
            wrapped.__senda_patched__ = True
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
            except Exception as exc:
                self._record_error(operation, kwargs, input_payload, start, exc)
                raise
            if kwargs.get("stream") is True:
                # stream=True は、イベントを読み終えた時点で量が確定する。読み終えたときに 1 件だけ記録する。
                return _UsageStream(
                    response,
                    lambda usage: self._record_success(operation, args, kwargs, input_payload, _StreamedMessage(kwargs.get("model"), usage), start),
                )
            self._record_success(operation, args, kwargs, input_payload, response, start)
            return response
        return wrapper

    def _wrap_async(self, original: Callable, operation: str) -> Callable:
        async def wrapper(obj, *args, **kwargs):
            cfg = get_config()
            start = time.perf_counter()
            input_payload = _input_payload(args, kwargs, cfg.capture_prompt, cfg.capture_hash)
            try:
                response = await original(obj, *args, **kwargs)
            except Exception as exc:
                self._record_error(operation, kwargs, input_payload, start, exc)
                raise
            if kwargs.get("stream") is True:
                return _UsageStream(
                    response,
                    lambda usage: self._record_success(operation, args, kwargs, input_payload, _StreamedMessage(kwargs.get("model"), usage), start),
                )
            self._record_success(operation, args, kwargs, input_payload, response, start)
            return response
        return wrapper

    def _wrap_stream(self, original: Callable, operation: str) -> Callable:
        """messages.stream() が返す管理オブジェクトを包み、抜けるときに、その時点の量を 1 件だけ記録する。"""
        def wrapper(obj, *args, **kwargs):
            cfg = get_config()
            start = time.perf_counter()
            input_payload = _input_payload(args, kwargs, cfg.capture_prompt, cfg.capture_hash)
            manager = original(obj, *args, **kwargs)
            return _UsageStreamManager(
                manager,
                lambda message: self._record_success(operation, args, kwargs, input_payload, message, start),
                lambda exc: self._record_error(operation, kwargs, input_payload, start, exc),
            )
        return wrapper

    def _record_error(self, operation: str, kwargs: dict, input_payload: dict, start: float, exc: BaseException) -> None:
        latency_ms = int((time.perf_counter() - start) * 1000)
        emit_event("llm.error", source={"component": "instrumentor", "sdk": "anthropic", "provider": "anthropic", "operation": operation}, data={"llm": {"provider": "anthropic", "operation": operation, "model": kwargs.get("model"), "input": input_payload}}, status="error", latency_ms=latency_ms, error={"type": exc.__class__.__name__, "message": str(exc)})

    def _record_success(self, operation: str, args: tuple, kwargs: dict, input_payload: dict, response: Any, start: float) -> None:
        cfg = get_config()

        # 観測の後処理は本来の呼び出しから隔離する。ここで失敗しても応答は返す。
        with audit_guard(operation):
            latency_ms = int((time.perf_counter() - start) * 1000)
            output_payload = _safe_response(response) if cfg.capture_response else {"response_hash": sha256_value(_safe_response(response))}
            messages_hash = sha256_value(kwargs.get("messages") or [])
            # 指示の載る場所は提供元と操作ごとに違う。応答系の要求では instructions と input に
            # 載り、位置引数で渡る形もある。名前を 2 つ決め打ちすると、その形の呼び出しでは
            # 行も組も空になり、判定が静かに止まる。場所の網羅は共通の収集へ任せる。
            _sources = collect_instruction_sources(kwargs, args)
            system_prompt_line_hashes = system_prompt_line_digests(*_sources)
            system_prompt_pair_hashes = system_prompt_pair_digests(*_sources)
            # 語の組が作れない日本語だけの指示のための文の署名。埋め込みと鍵が揃うときだけ出る。
            system_prompt_semantic_hashes = system_prompt_semantic_digests(*_sources)
            llm_data: dict[str, Any] = {"provider": "anthropic", "operation": operation, "model": kwargs.get("model"), "messages_hash": messages_hash, "input": input_payload, "output": output_payload}
            usage = _extract_usage(response)
            if usage:
                llm_data["usage"] = usage
            response_model = _extract_response_model(response)
            if response_model:
                llm_data["response_model"] = response_model
            if system_prompt_line_hashes:
                llm_data["system_prompt_line_hashes"] = system_prompt_line_hashes
            # 組は行と独立に載せる。行の内側へ入れると、行を出す条件を変えたときに組が
            # 黙って止まる。2 つは別々の導出で、片方が空でももう片方は成立する。
            if system_prompt_pair_hashes:
                llm_data["system_prompt_pair_hashes"] = system_prompt_pair_hashes
            if system_prompt_semantic_hashes:
                llm_data["system_prompt_semantic_hashes"] = system_prompt_semantic_hashes
            emit_event(
                "llm.request",
                source={"component": "instrumentor", "sdk": "anthropic", "provider": "anthropic", "operation": operation},
                data={"llm": llm_data},
                status="success",
                latency_ms=latency_ms,
            )
            offered = _offered_tool_names(kwargs)
            selected = _selected_tool_names(response)
            if offered and selected:
                alternatives = offered_alternatives(offered)
                for selected_tool in selected:
                    emit_event(
                        "agent.decision",
                        source={"component": "instrumentor", "sdk": "anthropic", "provider": "anthropic", "operation": operation},
                        data={
                            "alternatives": alternatives,
                            "selected_tool": selected_tool,
                        },
                        status="success",
                        latency_ms=latency_ms,
                    )


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
    """Messages API に渡された提示ツール集合の名前を取り出す。

    Anthropic は tools=[{"name":..., "input_schema":...}] の形をとる。
    """
    tools = kwargs.get("tools")
    names: list[str] = []
    if isinstance(tools, (list, tuple)):
        for tool in tools:
            name = ""
            if isinstance(tool, dict):
                name = str(tool.get("name") or "")
            if name:
                names.append(name)
    return names


def _selected_tool_names(response: Any) -> list[str]:
    """LLM 応答でモデルが選択したツール名を順序を保って全て取り出す。

    Anthropic は content に複数の tool_use ブロックが入りうる。最初の 1 件で
    打ち切ると後続の選択が監査から漏れるため、全て収集する。
    """
    resp = _safe_response(response)
    if not isinstance(resp, dict):
        return []
    names: list[str] = []
    content = resp.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                if name:
                    names.append(name)
    return names


def _safe_response(response: Any) -> Any:
    for attr in ("model_dump", "dict"):
        if hasattr(response, attr):
            with contextlib.suppress(Exception):
                return getattr(response, attr)()
    return str(response)


def _extract_usage(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None

    def _get(name: str) -> Any:
        return getattr(usage, name, None) if not isinstance(usage, dict) else usage.get(name)

    result: dict[str, int] = {}
    # input_tokens は、キャッシュの読みと作成を含まない入力。2 つのキャッシュのトークンは別の項目で、
    # プロバイダの使用量と同じ分け方で比べるために、捨てずに載せる。
    for name in USAGE_FIELDS:
        value = _get(name)
        if value is not None:
            result[name] = int(value)
    return result or None
