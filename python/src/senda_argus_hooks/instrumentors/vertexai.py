from __future__ import annotations

import re
import time
from typing import Any, Callable

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.model_identity import models_correspond
from senda_argus_hooks.core.runtime import emit_event, get_config

from .base import BaseInstrumentor


class VertexAIInstrumentor(BaseInstrumentor):
    """Instrument Vertex AI via vertexai.generative_models.

    vertexai.generative_models.GenerativeModel の _generate_content /
    _generate_content_async を割り込み点にして Gemini 等の推論呼び出しを観測する。
    公開の generate_content も ChatSession.send_message もこの私有メソッドに
    委譲するため、直接呼び出しとチャットセッションの両方が観測される。
    SDK の実装差に備え、私有メソッドが無い場合は公開メソッドに退避する。
    stream=True の応答はイテレータをラップし、消費完了後に最終チャンクの
    usage_metadata と自己申告モデルでイベントを送出する。
    """

    name = "vertexai"

    def __init__(self):
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        try:
            from vertexai.generative_models import GenerativeModel  # type: ignore
        except Exception:
            return False

        sync_name = "_generate_content" if hasattr(GenerativeModel, "_generate_content") else "generate_content"
        if hasattr(getattr(GenerativeModel, sync_name), "__senda_patched__"):
            return True
        original_sync = getattr(GenerativeModel, sync_name)
        wrapped_sync = self._wrap_sync(original_sync)
        setattr(wrapped_sync, "__senda_patched__", True)
        setattr(GenerativeModel, sync_name, wrapped_sync)
        self._patches.append((GenerativeModel, sync_name, original_sync))

        async_name = next(
            (n for n in ("_generate_content_async", "generate_content_async") if getattr(GenerativeModel, n, None) is not None),
            None,
        )
        if async_name is not None:
            original_async = getattr(GenerativeModel, async_name)
            wrapped_async = self._wrap_async(original_async)
            setattr(wrapped_async, "__senda_patched__", True)
            setattr(GenerativeModel, async_name, wrapped_async)
            self._patches.append((GenerativeModel, async_name, original_async))
        return True

    def _wrap_sync(self, original: Callable) -> Callable:
        def wrapper(model, *args, **kwargs):
            start = time.perf_counter()
            try:
                response = original(model, *args, **kwargs)
            except Exception as exc:
                _emit_error(model, args, kwargs, exc, start)
                raise
            if kwargs.get("stream") and hasattr(response, "__iter__"):
                return _wrap_stream(model, args, kwargs, response, start)
            _emit_request(model, args, kwargs, response, start)
            return response

        return wrapper

    def _wrap_async(self, original: Callable) -> Callable:
        async def wrapper(model, *args, **kwargs):
            start = time.perf_counter()
            try:
                response = await original(model, *args, **kwargs)
            except Exception as exc:
                _emit_error(model, args, kwargs, exc, start)
                raise
            if kwargs.get("stream") and hasattr(response, "__aiter__"):
                return _wrap_stream_async(model, args, kwargs, response, start)
            _emit_request(model, args, kwargs, response, start)
            return response

        return wrapper

    def uninstrument(self) -> bool:
        for target, method_name, original in self._patches:
            setattr(target, method_name, original)
        self._patches = []
        return True


def _model_name(model: Any) -> str:
    name = getattr(model, "_model_name", None)
    if isinstance(name, str) and name.strip():
        return name
    name = getattr(model, "model_name", None)
    if isinstance(name, str) and name.strip():
        return name
    return ""


def _model_core(identifier: str) -> str:
    """Vertex のリソースパス形式とネイティブモデル ID を比較可能な形に正規化する。

    リクエスト側は `publishers/google/models/gemini-1.5-pro` のようなリソースパス、
    レスポンス側の model_version は `gemini-1.5-pro-002` のような安定版 ID を
    自己申告する。スラッシュ区切りの最終セグメントから `@版数` と末尾の `-NNN`
    安定版接尾辞を除去した中核部分を比較単位にする。
    """
    core = identifier.strip().lower().split("/")[-1]
    core = core.split("@")[0]
    return re.sub(r"-\d{3}$", "", core)


def _usage(response: Any) -> dict[str, int] | None:
    metadata = getattr(response, "usage_metadata", None)
    if metadata is None:
        return None
    result: dict[str, int] = {}
    try:
        prompt = getattr(metadata, "prompt_token_count", None)
        candidates = getattr(metadata, "candidates_token_count", None)
        if prompt is not None:
            result["input_tokens"] = int(prompt)
        if candidates is not None:
            result["output_tokens"] = int(candidates)
    except (TypeError, ValueError):
        return None
    return result or None


def _input_payload(model: Any, args: tuple, kwargs: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    contents = args[0] if args else kwargs.get("contents")
    if cfg.capture_prompt:
        return {"contents": str(contents)}
    return {"contents_hash": sha256_value(str(contents))}


def _response_model_of(response: Any) -> str | None:
    """レスポンスが自己申告する実モデル名を取り出す。

    SDK バージョンによって wrapper が model_version を公開しない場合は
    raw proto から取る。
    """
    response_model = getattr(response, "model_version", None)
    if isinstance(response_model, str) and response_model.strip():
        return response_model
    response_model = getattr(getattr(response, "_raw_response", None), "model_version", None)
    if isinstance(response_model, str) and response_model.strip():
        return response_model
    return None


def _emit_llm_request(
    model: Any,
    args: tuple,
    kwargs: dict[str, Any],
    start: float,
    usage: dict[str, int] | None,
    response_model: str | None,
    output: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    """非 stream と stream の両経路で共有する llm.request イベントの組み立てと送出。"""
    name = _model_name(model)
    llm_data: dict[str, Any] = {
        "provider": "vertex_ai",
        "operation": "generate_content",
        "model": name,
        "input": _input_payload(model, args, kwargs),
    }
    if usage:
        llm_data["usage"] = usage
    # リソースパスと安定版 ID の表記差は偽陽性源のため、正規化して同一モデルと
    # 判定できる場合は response_model を送出しない。
    if response_model and not models_correspond(name, response_model, _model_core):
        llm_data["response_model"] = response_model
    llm_data["output"] = output
    if extra:
        llm_data.update(extra)
    emit_event(
        "llm.request",
        source={"component": "instrumentor", "sdk": "vertexai", "provider": "vertex_ai", "operation": "generate_content"},
        data={"llm": llm_data},
        status="success",
        latency_ms=int((time.perf_counter() - start) * 1000),
    )


def _emit_request(model: Any, args: tuple, kwargs: dict[str, Any], response: Any, start: float) -> None:
    cfg = get_config()
    if cfg.capture_response:
        output: dict[str, Any] = {"response": str(response)}
    else:
        output = {"response_hash": sha256_value(str(response))}
    _emit_llm_request(model, args, kwargs, start, _usage(response), _response_model_of(response), output)


def _new_stream_state() -> dict[str, Any]:
    return {"usage": {}, "response_model": None, "chunk_texts": []}


def _absorb_stream_chunk(state: dict[str, Any], chunk: Any) -> None:
    """stream チャンクから観測値を集約する。usage と自己申告モデルは最終チャンクに
    載るため、後に現れた値で上書きする。"""
    usage = _usage(chunk)
    if usage:
        state["usage"].update(usage)
    response_model = _response_model_of(chunk)
    if response_model:
        state["response_model"] = response_model
    state["chunk_texts"].append(str(chunk))


def _emit_stream(model: Any, args: tuple, kwargs: dict[str, Any], state: dict[str, Any], start: float) -> None:
    try:
        cfg = get_config()
        if cfg.capture_response:
            output: dict[str, Any] = {"response": state["chunk_texts"]}
        else:
            output = {"response_hash": sha256_value(state["chunk_texts"])}
        _emit_llm_request(
            model,
            args,
            kwargs,
            start,
            state["usage"] or None,
            state["response_model"],
            output,
            extra={"stream": True, "chunk_count": len(state["chunk_texts"])},
        )
    except Exception:
        pass


def _wrap_stream(model: Any, args: tuple, kwargs: dict[str, Any], iterator: Any, start: float) -> Any:
    """stream=True の応答イテレータをラップし、消費完了後にイベントを送出する。

    消費前に観測すると usage もモデルも欠落したイベントになる。途中で消費が
    中断された場合もそこまでの観測値で送出する。
    """
    state = _new_stream_state()

    def _gen():
        try:
            for chunk in iterator:
                try:
                    _absorb_stream_chunk(state, chunk)
                except Exception:
                    pass
                yield chunk
        finally:
            _emit_stream(model, args, kwargs, state, start)

    return _gen()


def _wrap_stream_async(model: Any, args: tuple, kwargs: dict[str, Any], iterator: Any, start: float) -> Any:
    state = _new_stream_state()

    async def _gen():
        try:
            async for chunk in iterator:
                try:
                    _absorb_stream_chunk(state, chunk)
                except Exception:
                    pass
                yield chunk
        finally:
            _emit_stream(model, args, kwargs, state, start)

    return _gen()


def _emit_error(model: Any, args: tuple, kwargs: dict[str, Any], exc: Exception, start: float) -> None:
    emit_event(
        "llm.error",
        source={"component": "instrumentor", "sdk": "vertexai", "provider": "vertex_ai", "operation": "generate_content"},
        data={"llm": {"provider": "vertex_ai", "operation": "generate_content", "model": _model_name(model), "input": _input_payload(model, args, kwargs)}},
        status="error",
        latency_ms=int((time.perf_counter() - start) * 1000),
        error={"type": exc.__class__.__name__, "message": str(exc)},
    )
