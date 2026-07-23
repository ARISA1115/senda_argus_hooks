from __future__ import annotations

import functools
import time
from typing import Any, Callable

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.response_meta import extract_response_model as _extract_response_model
from senda_argus_hooks.core.runtime import emit_event, get_config

from .base import BaseInstrumentor


class OllamaInstrumentor(BaseInstrumentor):
    """Instrument the official Ollama Python SDK.

    Supported in v0.5.0:
    - ollama.chat(...)
    - ollama.Client.chat(...)

    Current limitations:
    - stream=True is recorded as request/error events and as a response event
      when the SDK call returns successfully. Chunk-level streaming is not
      expanded into per-token events yet.
    - AsyncClient, generate(), and embeddings() are not instrumented yet.
    """

    name = "ollama"

    def __init__(self):
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        try:
            import ollama  # type: ignore
        except Exception:
            return False

        patched = False

        if hasattr(ollama, "chat") and not hasattr(getattr(ollama, "chat"), "__senda_patched__"):
            original = ollama.chat
            wrapped = self._wrap(original, "chat", client_type="module")
            setattr(wrapped, "__senda_patched__", True)
            ollama.chat = wrapped  # type: ignore[assignment]
            self._patches.append((ollama, "chat", original))
            patched = True

        client_cls = getattr(ollama, "Client", None)
        if client_cls is not None and hasattr(client_cls, "chat") and not hasattr(getattr(client_cls, "chat"), "__senda_patched__"):
            original = client_cls.chat
            wrapped = self._wrap(original, "Client.chat", client_type="client")
            setattr(wrapped, "__senda_patched__", True)
            setattr(client_cls, "chat", wrapped)
            self._patches.append((client_cls, "chat", original))
            patched = True

        return patched

    def _wrap(self, original: Callable, operation: str, client_type: str) -> Callable:
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            cfg = get_config()
            start = time.perf_counter()
            model = _extract_model(args, kwargs, client_type)
            messages = _extract_messages(args, kwargs, client_type)
            stream = bool(kwargs.get("stream", False))
            input_payload = _input_payload(args, kwargs, messages, cfg.capture_prompt, cfg.capture_hash)

            try:
                response = original(*args, **kwargs)
                latency_ms = int((time.perf_counter() - start) * 1000)
                output_payload = _safe_response(response) if cfg.capture_response else {"response_hash": sha256_value(_safe_response(response))}
                llm_data: dict[str, Any] = {
                    "provider": "ollama",
                    "operation": operation,
                    "model": model,
                    "input": input_payload,
                    "output": output_payload,
                    "metadata": {"client": client_type, "stream": stream},
                }
                # ModelSwapRule / ModelHashVerificationRule 用: レスポンスが自己申告した
                # 実モデルと、ローカル推論基盤が公開するモデルダイジェストを送出する。
                response_model = _extract_response_model(response)
                if response_model:
                    llm_data["response_model"] = response_model
                model_digest = _get_model_digest(model)
                if model_digest:
                    llm_data["model_digest"] = model_digest
                emit_event(
                    "llm.request",
                    source={"component": "instrumentor", "sdk": "ollama", "provider": "ollama", "operation": operation},
                    data={"llm": llm_data},
                    status="success",
                    latency_ms=latency_ms,
                )
                offered = _offered_tool_names(kwargs)
                selected = _selected_tool_names(response)
                if offered and selected:
                    alternatives = [{"name": name} for name in offered]
                    for selected_tool in selected:
                        emit_event(
                            "agent.decision",
                            source={"component": "instrumentor", "sdk": "ollama", "provider": "ollama", "operation": operation},
                            data={
                                "alternatives": alternatives,
                                "selected_tool": selected_tool,
                            },
                            status="success",
                            latency_ms=latency_ms,
                        )
                return response
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event(
                    "llm.error",
                    source={"component": "instrumentor", "sdk": "ollama", "provider": "ollama", "operation": operation},
                    data={
                        "llm": {
                            "provider": "ollama",
                            "operation": operation,
                            "model": model,
                            "input": input_payload,
                            "metadata": {"client": client_type, "stream": stream},
                        }
                    },
                    status="error",
                    latency_ms=latency_ms,
                    error={"type": exc.__class__.__name__, "message": str(exc)},
                )
                raise

        return wrapper

    def uninstrument(self) -> bool:
        for target, method_name, original in self._patches:
            setattr(target, method_name, original)
        self._patches = []
        return True


def _extract_model(args, kwargs, client_type: str) -> Any:
    if "model" in kwargs:
        return kwargs.get("model")
    if client_type == "module" and len(args) >= 1:
        return args[0]
    if client_type == "client" and len(args) >= 2:
        return args[1]
    return None


def _extract_messages(args, kwargs, client_type: str) -> Any:
    if "messages" in kwargs:
        return kwargs.get("messages")
    if client_type == "module" and len(args) >= 2:
        return args[1]
    if client_type == "client" and len(args) >= 3:
        return args[2]
    return None


def _input_payload(args, kwargs, messages, capture: bool, capture_hash: bool = True) -> dict[str, Any]:
    payload = {"args": args, "kwargs": kwargs}
    metadata = _llm_input_hash_metadata(args, kwargs, messages) if capture_hash else {}
    if capture:
        return {**payload, **metadata}
    return {"input_hash": sha256_value(payload), **metadata}


def _llm_input_hash_metadata(args, kwargs, messages) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "args_hash": sha256_value(args),
        "kwargs_hash": sha256_value(kwargs),
    }
    if isinstance(messages, tuple):
        messages = list(messages)
    if isinstance(messages, list):
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


def _offered_tool_names(kwargs) -> list[str]:
    """ollama.chat に渡された提示ツール集合の名前を取り出す。

    ollama は OpenAI 互換の tools=[{"type":"function","function":{"name":...}}] を受け取る。
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


def _selected_tool_names(response: Any) -> list[str]:
    """ollama 応答でモデルが選択したツール名を順序を保って全て取り出す。

    ollama は message.tool_calls[].function.name に選択を載せる。並列選択を漏らさず全て収集する。
    """
    resp = _safe_response(response)
    if not isinstance(resp, dict):
        return []
    names: list[str] = []
    message = resp.get("message")
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, dict):
                fn = call.get("function")
                name = str(fn.get("name") or "") if isinstance(fn, dict) else ""
                if name:
                    names.append(name)
    return names


def _safe_response(response: Any) -> Any:
    for attr in ("model_dump", "dict"):
        if hasattr(response, attr):
            try:
                return getattr(response, attr)()
            except Exception:
                pass
    if isinstance(response, (dict, list, str, int, float, bool)) or response is None:
        return response
    return str(response)


_MODEL_DIGEST_CACHE: dict[str, str] = {}


def _get_model_digest(model: Any) -> str | None:
    """ollama の list API からモデルマニフェストの sha256 ダイジェストを取得する。

    ローカル推論基盤である Ollama はモデルごとのダイジェストを公開しており、
    Argus 側の ModelHashVerificationRule が重み差し替えとピン留めハッシュ不一致を
    検知するための一次情報になる。呼び出しごとの追加レイテンシを避けるため
    プロセス内でキャッシュし、取得失敗時は None を返して観測を阻害しない。
    """
    if not isinstance(model, str) or not model:
        return None
    cached = _MODEL_DIGEST_CACHE.get(model) or _MODEL_DIGEST_CACHE.get(f"{model}:latest")
    if cached is not None:
        return cached
    try:
        import ollama  # type: ignore

        listing = ollama.list()
        models = listing.get("models") if isinstance(listing, dict) else getattr(listing, "models", None)
        for m in models or []:
            if isinstance(m, dict):
                name = m.get("model") or m.get("name")
                digest = m.get("digest")
            else:
                name = getattr(m, "model", None) or getattr(m, "name", None)
                digest = getattr(m, "digest", None)
            if isinstance(name, str) and isinstance(digest, str) and name and digest:
                _MODEL_DIGEST_CACHE[name] = digest
    except Exception:
        return None
    return _MODEL_DIGEST_CACHE.get(model) or _MODEL_DIGEST_CACHE.get(f"{model}:latest")
