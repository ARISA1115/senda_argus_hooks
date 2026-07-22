from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from senda_argus_hooks.core.hashing import derive_embedding_sketch, sha256_value
from senda_argus_hooks.core.identity import derive_embedding_purpose_id, derive_retrieval_purpose_id
from senda_argus_hooks.core.runtime import emit_event, get_config


class SendaArgusLlamaIndexCallbackHandler:
    """Best-effort LlamaIndex-style event handler.

    LlamaIndex releases expose observability/instrumentation surfaces that may
    change over time. This lightweight handler is intentionally duck-typed: it
    can be called from tests, adapters, or application glue code without making
    LlamaIndex a hard dependency.
    """

    def __init__(self, *, framework: str = "llamaindex") -> None:
        self.framework = framework
        self._starts: dict[str, float] = {}

    def on_retrieval_start(self, query: Any, **kwargs: Any) -> None:
        run_id = _run_key(kwargs)
        self._starts[run_id] = time.perf_counter()
        payload = _retrieval_payload(self.framework, query, kwargs)
        emit_event(
            "retrieval.requested",
            source={"component": "integration", "sdk": self.framework, "operation": "on_retrieval_start"},
            data={"retrieval": payload},
            status="start",
            purpose_id=payload["purpose_id"],
        )

    def on_retrieval_end(self, results: Any, **kwargs: Any) -> None:
        latency_ms = _latency_ms(self._starts.pop(_run_key(kwargs), None))
        payload = _retrieval_result_payload(self.framework, results, kwargs)
        emit_event(
            "retrieval.completed",
            source={"component": "integration", "sdk": self.framework, "operation": "on_retrieval_end"},
            data={"retrieval": payload},
            status="success",
            latency_ms=latency_ms,
            purpose_id=payload["purpose_id"],
        )

    def on_retrieval_error(self, error: BaseException, **kwargs: Any) -> None:
        latency_ms = _latency_ms(self._starts.pop(_run_key(kwargs), None))
        payload = _retrieval_payload(self.framework, kwargs.get("query"), kwargs)
        emit_event(
            "retrieval.failed",
            source={"component": "integration", "sdk": self.framework, "operation": "on_retrieval_error"},
            data={"retrieval": payload},
            status="error",
            latency_ms=latency_ms,
            error={"type": error.__class__.__name__, "message": str(error)},
            purpose_id=payload["purpose_id"],
        )

    def on_embedding_start(self, input_text: Any, **kwargs: Any) -> None:
        run_id = _run_key(kwargs)
        self._starts[run_id] = time.perf_counter()
        payload = _embedding_payload(self.framework, input_text, kwargs)
        emit_event(
            "embedding.requested",
            source={"component": "integration", "sdk": self.framework, "operation": "on_embedding_start"},
            data={"embedding": payload},
            status="start",
            purpose_id=payload["purpose_id"],
        )

    def on_embedding_end(self, vector: Any, **kwargs: Any) -> None:
        latency_ms = _latency_ms(self._starts.pop(_run_key(kwargs), None))
        payload = _embedding_result_payload(self.framework, vector, kwargs)
        emit_event(
            "embedding.completed",
            source={"component": "integration", "sdk": self.framework, "operation": "on_embedding_end"},
            data={"embedding": payload},
            status="success",
            latency_ms=latency_ms,
            purpose_id=payload["purpose_id"],
        )

    def on_embedding_error(self, error: BaseException, **kwargs: Any) -> None:
        latency_ms = _latency_ms(self._starts.pop(_run_key(kwargs), None))
        payload = _embedding_payload(self.framework, kwargs.get("input_text"), kwargs)
        emit_event(
            "embedding.failed",
            source={"component": "integration", "sdk": self.framework, "operation": "on_embedding_error"},
            data={"embedding": payload},
            status="error",
            latency_ms=latency_ms,
            error={"type": error.__class__.__name__, "message": str(error)},
            purpose_id=payload["purpose_id"],
        )


class RAGInstrumentation:
    """Instance-local RAG instrumentation handle.

    ``instrument_rag()`` returns this object so callers can restore patched
    retriever, embedding, and query engine methods when needed.
    """

    def __init__(self) -> None:
        self._patches: list[tuple[Any, str, Any]] = []

    def patch(self, obj: Any, method_name: str, wrapper_factory: Any) -> bool:
        if obj is None or not hasattr(obj, method_name):
            return False
        original = getattr(obj, method_name)
        if getattr(original, "_senda_argus_wrapped", False):
            return False
        wrapped = wrapper_factory(original)
        try:
            setattr(wrapped, "_senda_argus_wrapped", True)
        except Exception:
            pass
        self._patches.append((obj, method_name, original))
        setattr(obj, method_name, wrapped)
        return True

    def installed(self) -> dict[str, list[str]]:
        installed: dict[str, list[str]] = {}
        for obj, method_name, _original in self._patches:
            installed.setdefault(obj.__class__.__name__, []).append(method_name)
        return installed

    def uninstrument(self) -> None:
        for obj, method_name, original in reversed(self._patches):
            try:
                setattr(obj, method_name, original)
            except Exception:
                pass
        self._patches.clear()


def instrument_rag(
    *,
    retriever: Any | None = None,
    embed_model: Any | None = None,
    query_engine: Any | None = None,
    framework: str = "llamaindex",
    retriever_name: str | None = None,
    retriever_type: str | None = None,
    index_name: str | None = None,
    collection_name: str | None = None,
    vector_store: str | None = None,
    top_k: int | None = None,
    target: str | None = None,
    provider: str | None = None,
    embedding_model: str | None = None,
    query_engine_name: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    source_type: str | None = None,
) -> RAGInstrumentation:
    """Enable RAG observability with one function call.

    This patches only the supplied component instances. Existing application
    code can keep calling normal framework methods such as
    ``retriever.retrieve(...)``, ``embed_model.get_text_embedding(...)``, and
    ``query_engine.query(...)``.
    """
    handle = RAGInstrumentation()

    if retriever is not None:
        base_retriever_name = _name(retriever, retriever_name)
        handle.patch(
            retriever,
            "retrieve",
            lambda original: lambda query, *args, **kwargs: _call_retriever_method_with_argus(
                original,
                query,
                *args,
                framework=framework,
                retriever_name=base_retriever_name,
                retriever_type=retriever_type,
                index_name=index_name,
                collection_name=collection_name,
                vector_store=vector_store,
                top_k=top_k,
                target=target,
                source=source,
                source_url=source_url,
                source_type=source_type,
                operation="retrieve",
                **kwargs,
            ),
        )
        handle.patch(
            retriever,
            "aretrieve",
            lambda original: lambda query, *args, **kwargs: _acall_retriever_method_with_argus(
                original,
                query,
                *args,
                framework=framework,
                retriever_name=base_retriever_name,
                retriever_type=retriever_type,
                index_name=index_name,
                collection_name=collection_name,
                vector_store=vector_store,
                top_k=top_k,
                target=target,
                source=source,
                source_url=source_url,
                source_type=source_type,
                operation="aretrieve",
                **kwargs,
            ),
        )

    if embed_model is not None:
        base_model = _model_name(embed_model, embedding_model)
        handle.patch(
            embed_model,
            "get_text_embedding",
            lambda original: lambda text, *args, **kwargs: _call_embedding_method_with_argus(
                original,
                text,
                *args,
                framework=framework,
                provider=provider,
                model=base_model,
                input_count=1,
                operation="get_text_embedding",
                **kwargs,
            ),
        )
        handle.patch(
            embed_model,
            "get_text_embeddings",
            lambda original: lambda texts, *args, **kwargs: _call_embedding_method_with_argus(
                original,
                texts,
                *args,
                framework=framework,
                provider=provider,
                model=base_model,
                input_count=len(texts) if isinstance(texts, list) else None,
                operation="get_text_embeddings",
                **kwargs,
            ),
        )
        handle.patch(
            embed_model,
            "aget_text_embedding",
            lambda original: lambda text, *args, **kwargs: _acall_embedding_method_with_argus(
                original,
                text,
                *args,
                framework=framework,
                provider=provider,
                model=base_model,
                input_count=1,
                operation="aget_text_embedding",
                **kwargs,
            ),
        )
        handle.patch(
            embed_model,
            "aget_text_embeddings",
            lambda original: lambda texts, *args, **kwargs: _acall_embedding_method_with_argus(
                original,
                texts,
                *args,
                framework=framework,
                provider=provider,
                model=base_model,
                input_count=len(texts) if isinstance(texts, list) else None,
                operation="aget_text_embeddings",
                **kwargs,
            ),
        )

    if query_engine is not None:
        base_query_engine_name = _name(query_engine, query_engine_name)
        handle.patch(
            query_engine,
            "query",
            lambda original: lambda query, *args, **kwargs: _call_query_engine_method_with_argus(
                original,
                query,
                *args,
                framework=framework,
                query_engine_name=base_query_engine_name,
                operation="query",
                **kwargs,
            ),
        )
        handle.patch(
            query_engine,
            "aquery",
            lambda original: lambda query, *args, **kwargs: _acall_query_engine_method_with_argus(
                original,
                query,
                *args,
                framework=framework,
                query_engine_name=base_query_engine_name,
                operation="aquery",
                **kwargs,
            ),
        )

    return handle


def _call_retriever_method_with_argus(
    func: Any,
    query: Any,
    *args: Any,
    framework: str,
    retriever_name: str | None = None,
    retriever_type: str | None = None,
    index_name: str | None = None,
    collection_name: str | None = None,
    vector_store: str | None = None,
    top_k: int | None = None,
    target: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    source_type: str | None = None,
    operation: str = "retrieve",
    **kwargs: Any,
) -> Any:
    meta = _metadata(
        retriever_name=retriever_name,
        retriever_type=retriever_type,
        index_name=index_name,
        collection_name=collection_name,
        vector_store=vector_store,
        top_k=top_k,
        target=target,
        source=source,
        source_url=source_url,
        source_type=source_type,
    )
    requested = _retrieval_payload(framework, query, meta)
    start = time.perf_counter()
    emit_event(
        "retrieval.requested",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"retrieval": requested},
        status="start",
        purpose_id=requested["purpose_id"],
    )
    try:
        result = func(query, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "retrieval.failed",
            source={"component": "integration", "sdk": framework, "operation": operation},
            data={"retrieval": requested},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
            purpose_id=requested["purpose_id"],
        )
        raise
    completed = _retrieval_result_payload(framework, result, meta | {"query": query})
    emit_event(
        "retrieval.completed",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"retrieval": completed},
        status="success",
        latency_ms=_latency_ms(start),
        purpose_id=completed["purpose_id"],
    )
    return result


async def _acall_retriever_method_with_argus(
    func: Any,
    query: Any,
    *args: Any,
    framework: str,
    retriever_name: str | None = None,
    retriever_type: str | None = None,
    index_name: str | None = None,
    collection_name: str | None = None,
    vector_store: str | None = None,
    top_k: int | None = None,
    target: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    source_type: str | None = None,
    operation: str = "aretrieve",
    **kwargs: Any,
) -> Any:
    """func が待機不要のコルーチンを返す非同期メソッドである前提で await してから完了イベントを送出する。

    同期版に処理を委譲すると func(...) の戻り値がコルーチンのまま result_hash/
    context_hash に使われ、実行前の未完了状態を completed として誤報告するため、
    ここでは同期版のロジックを await 込みで再実装する。
    """
    meta = _metadata(
        retriever_name=retriever_name,
        retriever_type=retriever_type,
        index_name=index_name,
        collection_name=collection_name,
        vector_store=vector_store,
        top_k=top_k,
        target=target,
        source=source,
        source_url=source_url,
        source_type=source_type,
    )
    requested = _retrieval_payload(framework, query, meta)
    start = time.perf_counter()
    emit_event(
        "retrieval.requested",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"retrieval": requested},
        status="start",
        purpose_id=requested["purpose_id"],
    )
    try:
        result = await func(query, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "retrieval.failed",
            source={"component": "integration", "sdk": framework, "operation": operation},
            data={"retrieval": requested},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
            purpose_id=requested["purpose_id"],
        )
        raise
    completed = _retrieval_result_payload(framework, result, meta | {"query": query})
    emit_event(
        "retrieval.completed",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"retrieval": completed},
        status="success",
        latency_ms=_latency_ms(start),
        purpose_id=completed["purpose_id"],
    )
    return result


def _call_embedding_method_with_argus(
    func: Any,
    input_text: Any,
    *args: Any,
    framework: str,
    provider: str | None = None,
    model: str | None = None,
    input_count: int | None = None,
    operation: str = "embedding",
    **kwargs: Any,
) -> Any:
    meta = {"provider": provider, "model": model, "input_count": input_count}
    requested = _embedding_payload(framework, input_text, meta)
    start = time.perf_counter()
    emit_event(
        "embedding.requested",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"embedding": requested},
        status="start",
        purpose_id=requested["purpose_id"],
    )
    try:
        vector = func(input_text, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "embedding.failed",
            source={"component": "integration", "sdk": framework, "operation": operation},
            data={"embedding": requested},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
            purpose_id=requested["purpose_id"],
        )
        raise
    completed = _embedding_result_payload(framework, vector, meta | {"input_text": input_text})
    emit_event(
        "embedding.completed",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"embedding": completed},
        status="success",
        latency_ms=_latency_ms(start),
        purpose_id=completed["purpose_id"],
    )
    return vector


async def _acall_embedding_method_with_argus(
    func: Any,
    input_text: Any,
    *args: Any,
    framework: str,
    provider: str | None = None,
    model: str | None = None,
    input_count: int | None = None,
    operation: str = "aembedding",
    **kwargs: Any,
) -> Any:
    """同期版と同じ理由で、func を await してから completed イベントを送出する。"""
    meta = {"provider": provider, "model": model, "input_count": input_count}
    requested = _embedding_payload(framework, input_text, meta)
    start = time.perf_counter()
    emit_event(
        "embedding.requested",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"embedding": requested},
        status="start",
        purpose_id=requested["purpose_id"],
    )
    try:
        vector = await func(input_text, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "embedding.failed",
            source={"component": "integration", "sdk": framework, "operation": operation},
            data={"embedding": requested},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
            purpose_id=requested["purpose_id"],
        )
        raise
    completed = _embedding_result_payload(framework, vector, meta | {"input_text": input_text})
    emit_event(
        "embedding.completed",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"embedding": completed},
        status="success",
        latency_ms=_latency_ms(start),
        purpose_id=completed["purpose_id"],
    )
    return vector


def _call_query_engine_method_with_argus(
    func: Any,
    query: Any,
    *args: Any,
    framework: str,
    query_engine_name: str | None = None,
    operation: str = "query",
    **kwargs: Any,
) -> Any:
    start = time.perf_counter()
    data = {
        "framework": framework,
        "query_engine": query_engine_name or "unknown",
        "query_hash": sha256_value(query),
    }
    if _capture_query():
        data["query"] = _safe_value(query)
    emit_event(
        "rag.query.started",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"rag": data},
        status="start",
    )
    try:
        result = func(query, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "rag.query.failed",
            source={"component": "integration", "sdk": framework, "operation": operation},
            data={"rag": data},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
        )
        raise
    completed = {**data, "result_hash": sha256_value(result)}
    context_hash, context_count = _context_hash_and_count(result)
    if context_hash is not None:
        completed["context_hash"] = context_hash
    if context_count is not None:
        completed["context_count"] = context_count
    if get_config().capture_result:
        completed["result"] = _safe_value(result)
    emit_event(
        "rag.query.completed",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"rag": completed},
        status="success",
        latency_ms=_latency_ms(start),
    )
    return result


async def _acall_query_engine_method_with_argus(
    func: Any,
    query: Any,
    *args: Any,
    framework: str,
    query_engine_name: str | None = None,
    operation: str = "aquery",
    **kwargs: Any,
) -> Any:
    """同期版と同じ理由で、func を await してから completed イベントを送出する。"""
    start = time.perf_counter()
    data = {
        "framework": framework,
        "query_engine": query_engine_name or "unknown",
        "query_hash": sha256_value(query),
    }
    if _capture_query():
        data["query"] = _safe_value(query)
    emit_event(
        "rag.query.started",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"rag": data},
        status="start",
    )
    try:
        result = await func(query, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "rag.query.failed",
            source={"component": "integration", "sdk": framework, "operation": operation},
            data={"rag": data},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
        )
        raise
    completed = {**data, "result_hash": sha256_value(result)}
    context_hash, context_count = _context_hash_and_count(result)
    if context_hash is not None:
        completed["context_hash"] = context_hash
    if context_count is not None:
        completed["context_count"] = context_count
    if get_config().capture_result:
        completed["result"] = _safe_value(result)
    emit_event(
        "rag.query.completed",
        source={"component": "integration", "sdk": framework, "operation": operation},
        data={"rag": completed},
        status="success",
        latency_ms=_latency_ms(start),
    )
    return result


def retrieve_with_argus(
    retriever: Any,
    query: Any,
    *args: Any,
    framework: str = "llamaindex",
    retriever_name: str | None = None,
    retriever_type: str | None = None,
    index_name: str | None = None,
    collection_name: str | None = None,
    vector_store: str | None = None,
    top_k: int | None = None,
    target: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    source_type: str | None = None,
    method: str = "retrieve",
    **kwargs: Any,
) -> Any:
    """Call a retriever and emit retrieval lifecycle events.

    This wrapper works with LlamaIndex retrievers and simple duck-typed objects
    that expose a `retrieve(query, ...)` method. It does not require LlamaIndex
    at import time.
    """
    meta = _metadata(
        retriever_name=_name(retriever, retriever_name),
        retriever_type=retriever_type,
        index_name=index_name,
        collection_name=collection_name,
        vector_store=vector_store,
        top_k=top_k,
        target=target,
        source=source,
        source_url=source_url,
        source_type=source_type,
    )
    requested = _retrieval_payload(framework, query, meta)
    start = time.perf_counter()
    emit_event(
        "retrieval.requested",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"retrieval": requested},
        status="start",
        purpose_id=requested["purpose_id"],
    )
    try:
        result = getattr(retriever, method)(query, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "retrieval.failed",
            source={"component": "integration", "sdk": framework, "operation": method},
            data={"retrieval": requested},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
            purpose_id=requested["purpose_id"],
        )
        raise
    completed = _retrieval_result_payload(framework, result, meta | {"query": query})
    emit_event(
        "retrieval.completed",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"retrieval": completed},
        status="success",
        latency_ms=_latency_ms(start),
        purpose_id=completed["purpose_id"],
    )
    return result


async def aretrieve_with_argus(
    retriever: Any,
    query: Any,
    *args: Any,
    framework: str = "llamaindex",
    retriever_name: str | None = None,
    retriever_type: str | None = None,
    index_name: str | None = None,
    collection_name: str | None = None,
    vector_store: str | None = None,
    top_k: int | None = None,
    target: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    source_type: str | None = None,
    method: str = "aretrieve",
    **kwargs: Any,
) -> Any:
    meta = _metadata(
        retriever_name=_name(retriever, retriever_name),
        retriever_type=retriever_type,
        index_name=index_name,
        collection_name=collection_name,
        vector_store=vector_store,
        top_k=top_k,
        target=target,
        source=source,
        source_url=source_url,
        source_type=source_type,
    )
    requested = _retrieval_payload(framework, query, meta)
    start = time.perf_counter()
    emit_event(
        "retrieval.requested",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"retrieval": requested},
        status="start",
        purpose_id=requested["purpose_id"],
    )
    try:
        result = await getattr(retriever, method)(query, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "retrieval.failed",
            source={"component": "integration", "sdk": framework, "operation": method},
            data={"retrieval": requested},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
            purpose_id=requested["purpose_id"],
        )
        raise
    completed = _retrieval_result_payload(framework, result, meta | {"query": query})
    emit_event(
        "retrieval.completed",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"retrieval": completed},
        status="success",
        latency_ms=_latency_ms(start),
        purpose_id=completed["purpose_id"],
    )
    return result


def embed_text_with_argus(
    embed_model: Any,
    text: str,
    *args: Any,
    framework: str = "llamaindex",
    provider: str | None = None,
    model: str | None = None,
    method: str = "get_text_embedding",
    **kwargs: Any,
) -> Any:
    start = time.perf_counter()
    requested = _embedding_payload(framework, text, {"provider": provider, "model": _model_name(embed_model, model), "input_count": 1})
    emit_event(
        "embedding.requested",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"embedding": requested},
        status="start",
        purpose_id=requested["purpose_id"],
    )
    try:
        vector = getattr(embed_model, method)(text, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "embedding.failed",
            source={"component": "integration", "sdk": framework, "operation": method},
            data={"embedding": requested},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
            purpose_id=requested["purpose_id"],
        )
        raise
    completed = _embedding_result_payload(framework, vector, {"provider": provider, "model": _model_name(embed_model, model), "input_text": text})
    emit_event(
        "embedding.completed",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"embedding": completed},
        status="success",
        latency_ms=_latency_ms(start),
        purpose_id=completed["purpose_id"],
    )
    return vector


def embed_texts_with_argus(
    embed_model: Any,
    texts: list[str],
    *args: Any,
    framework: str = "llamaindex",
    provider: str | None = None,
    model: str | None = None,
    method: str = "get_text_embeddings",
    **kwargs: Any,
) -> Any:
    start = time.perf_counter()
    requested = _embedding_payload(framework, texts, {"provider": provider, "model": _model_name(embed_model, model), "input_count": len(texts)})
    emit_event(
        "embedding.requested",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"embedding": requested},
        status="start",
        purpose_id=requested["purpose_id"],
    )
    try:
        vectors = getattr(embed_model, method)(texts, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "embedding.failed",
            source={"component": "integration", "sdk": framework, "operation": method},
            data={"embedding": requested},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
            purpose_id=requested["purpose_id"],
        )
        raise
    completed = _embedding_result_payload(framework, vectors, {"provider": provider, "model": _model_name(embed_model, model), "input_text": texts})
    emit_event(
        "embedding.completed",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"embedding": completed},
        status="success",
        latency_ms=_latency_ms(start),
        purpose_id=completed["purpose_id"],
    )
    return vectors


def query_with_argus(
    query_engine: Any,
    query: Any,
    *args: Any,
    framework: str = "llamaindex",
    query_engine_name: str | None = None,
    method: str = "query",
    **kwargs: Any,
) -> Any:
    start = time.perf_counter()
    data = {
        "framework": framework,
        "query_engine": _name(query_engine, query_engine_name),
        "query_hash": sha256_value(query),
    }
    if _capture_query():
        data["query"] = _safe_value(query)
    emit_event(
        "rag.query.started",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"rag": data},
        status="start",
    )
    try:
        result = getattr(query_engine, method)(query, *args, **kwargs)
    except Exception as exc:
        emit_event(
            "rag.query.failed",
            source={"component": "integration", "sdk": framework, "operation": method},
            data={"rag": data},
            status="error",
            latency_ms=_latency_ms(start),
            error={"type": exc.__class__.__name__, "message": str(exc)},
        )
        raise
    completed = {**data, "result_hash": sha256_value(result)}
    context_hash, context_count = _context_hash_and_count(result)
    if context_hash is not None:
        completed["context_hash"] = context_hash
    if context_count is not None:
        completed["context_count"] = context_count
    if get_config().capture_result:
        completed["result"] = _safe_value(result)
    emit_event(
        "rag.query.completed",
        source={"component": "integration", "sdk": framework, "operation": method},
        data={"rag": completed},
        status="success",
        latency_ms=_latency_ms(start),
    )
    return result


def _metadata(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


def _retrieval_payload(framework: str, query: Any, meta: dict[str, Any]) -> dict[str, Any]:
    purpose_id = _retrieval_purpose(framework, meta)
    payload = {
        "framework": framework,
        "retriever_name": meta.get("retriever_name") or "unknown",
        "retriever_type": meta.get("retriever_type") or "unknown",
        "query_hash": sha256_value(query),
        "top_k": meta.get("top_k"),
        "index_name": meta.get("index_name"),
        "collection_name": meta.get("collection_name"),
        "vector_store": meta.get("vector_store"),
        "target": meta.get("target"),
        "source": meta.get("source"),
        "source_url": meta.get("source_url"),
        "source_type": meta.get("source_type"),
        "purpose_id": purpose_id,
    }
    if _capture_query():
        payload["query"] = _safe_value(query)
    return {k: v for k, v in payload.items() if v is not None}


def _retrieval_result_payload(framework: str, results: Any, meta: dict[str, Any]) -> dict[str, Any]:
    items = _as_list(results)
    scores = [_score(item) for item in items]
    scores = [score for score in scores if score is not None]
    document_ids = [_document_id(item) for item in items]
    chunk_ids = [_chunk_id(item) for item in items]
    payload = _retrieval_payload(framework, meta.get("query"), meta)
    payload.update(
        {
            "result_count": len(items),
            "document_ids_hash": sha256_value([x for x in document_ids if x]),
            "chunk_ids_hash": sha256_value([x for x in chunk_ids if x]),
            "score_min": min(scores) if scores else None,
            "score_max": max(scores) if scores else None,
            "result_hash": sha256_value(results),
        }
    )
    if get_config().capture_result:
        payload["result"] = _safe_value(results)
    return {k: v for k, v in payload.items() if v is not None}


def _embedding_payload(framework: str, input_text: Any, meta: dict[str, Any]) -> dict[str, Any]:
    purpose_id = derive_embedding_purpose_id(framework=framework, provider=meta.get("provider"), model=meta.get("model"), embedding_type="text")
    payload = {
        "framework": framework,
        "provider": meta.get("provider"),
        "model": meta.get("model") or "unknown",
        "input_hash": sha256_value(input_text),
        "input_count": meta.get("input_count") or _input_count(input_text),
        "input_length": _input_length(input_text),
        "purpose_id": purpose_id,
    }
    if _capture_query():
        payload["input"] = _safe_value(input_text)
    return {k: v for k, v in payload.items() if v is not None}


def _embedding_result_payload(framework: str, vector: Any, meta: dict[str, Any]) -> dict[str, Any]:
    payload = _embedding_payload(framework, meta.get("input_text"), meta)
    payload.update(
        {
            "vector_dimension": _vector_dimension(vector),
            "vector_count": _vector_count(vector),
            "vector_hash": sha256_value(vector),
            "vector_sketch": derive_embedding_sketch(_representative_vector(vector)),
        }
    )
    return {k: v for k, v in payload.items() if v is not None}


def _representative_vector(vector: Any) -> list | None:
    """EMBEDDING_CLUSTERING_ANOMALY のスケッチ計算に使う代表ベクトルを1件取り出す。

    get_text_embeddings のようなバッチ呼び出しでは複数ベクトルが返るが、
    クラスタリングは1呼び出しにつき1ベクトルを前提とするため先頭を代表として使う。
    """
    if isinstance(vector, list) and vector and all(isinstance(v, (int, float)) for v in vector):
        return vector
    if isinstance(vector, list) and vector and isinstance(vector[0], (list, tuple)):
        return list(vector[0])
    return None


def _retrieval_purpose(framework: str, meta: dict[str, Any]) -> str:
    return derive_retrieval_purpose_id(
        framework=framework,
        retriever_name=meta.get("retriever_name"),
        retriever_type=meta.get("retriever_type"),
        index_name=meta.get("index_name"),
        collection_name=meta.get("collection_name"),
        vector_store=meta.get("vector_store"),
        target=meta.get("target"),
    )


def _capture_query() -> bool:
    return bool(get_config().capture_arguments)


def _run_key(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("run_id") or kwargs.get("event_id") or "default")


def _latency_ms(start: float | None) -> int | None:
    return int((time.perf_counter() - start) * 1000) if start is not None else None


def _name(obj: Any, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for attr in ("name", "_name", "class_name"):
        value = getattr(obj, attr, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if value:
            return str(value)
    return obj.__class__.__name__ if obj is not None else "unknown"


def _model_name(obj: Any, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for attr in ("model", "model_name", "_model", "_model_name"):
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    return _name(obj)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        try:
            return list(value)
        except TypeError:
            return [value]
    return [value]


def _score(item: Any) -> float | None:
    for attr in ("score", "similarity", "similarity_score"):
        value = getattr(item, attr, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    if isinstance(item, dict):
        for key in ("score", "similarity", "similarity_score"):
            if item.get(key) is not None:
                try:
                    return float(item[key])
                except (TypeError, ValueError):
                    return None
    return None


def _node(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("node") or item
    return getattr(item, "node", item)


def _node_text(item: Any) -> str | None:
    node = _node(item)
    get_content = getattr(node, "get_content", None)
    if callable(get_content):
        try:
            text = get_content()
            if text is not None:
                return str(text)
        except Exception:
            pass
    text = getattr(node, "text", None)
    if text is not None:
        return str(text)
    if isinstance(node, dict):
        text = node.get("text")
        if text is not None:
            return str(text)
    return None


def _context_hash_and_count(result: Any) -> tuple[str | None, int | None]:
    """クエリエンジンの応答から実際に使用された検索コンテキストの hash と件数を抽出する。

    LlamaIndex のクエリエンジン応答は source_nodes (検索されコンテキストとして
    LLM に渡されたノード集合) を持つ。この情報がなければ context drift/count
    異常検知ルールは一切発火できないため、result を黒箱のまま扱わず抽出する。
    """
    source_nodes = getattr(result, "source_nodes", None)
    if source_nodes is None and isinstance(result, dict):
        source_nodes = result.get("source_nodes")
    if source_nodes is None:
        return None, None
    items = _as_list(source_nodes)
    texts = [_node_text(item) for item in items]
    return sha256_value(texts), len(items)


def _document_id(item: Any) -> str | None:
    node = _node(item)
    for attr in ("ref_doc_id", "doc_id", "document_id"):
        value = getattr(node, attr, None)
        if value:
            return str(value)
    if isinstance(node, dict):
        for key in ("ref_doc_id", "doc_id", "document_id"):
            if node.get(key):
                return str(node[key])
    metadata = getattr(node, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("ref_doc_id", "doc_id", "document_id"):
            if metadata.get(key):
                return str(metadata[key])
    return None


def _chunk_id(item: Any) -> str | None:
    node = _node(item)
    for attr in ("node_id", "id_", "id", "chunk_id"):
        value = getattr(node, attr, None)
        if value:
            return str(value)
    if isinstance(node, dict):
        for key in ("node_id", "id_", "id", "chunk_id"):
            if node.get(key):
                return str(node[key])
    return None


def _input_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 1


def _input_length(value: Any) -> int | None:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(len(v) for v in value if isinstance(v, str))
    return None


def _vector_dimension(vector: Any) -> int | None:
    if isinstance(vector, list) and vector and all(isinstance(v, (int, float)) for v in vector):
        return len(vector)
    if isinstance(vector, tuple) and vector and all(isinstance(v, (int, float)) for v in vector):
        return len(vector)
    if isinstance(vector, list) and vector and isinstance(vector[0], (list, tuple)):
        return len(vector[0])
    return None


def _vector_count(vector: Any) -> int | None:
    if isinstance(vector, list) and vector and isinstance(vector[0], (list, tuple)):
        return len(vector)
    if isinstance(vector, list) and vector and all(isinstance(v, (int, float)) for v in vector):
        return 1
    return None


def _safe_value(value: Any) -> Any:
    for attr in ("model_dump", "dict"):
        if hasattr(value, attr):
            try:
                return getattr(value, attr)()
            except Exception:
                pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in value.items()}
    return str(value)
