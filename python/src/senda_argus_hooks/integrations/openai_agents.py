from __future__ import annotations

import inspect
import time
from typing import Any, Callable

from senda_argus_hooks.core.instruction_files import (
    collect_instruction_sources,
    system_prompt_line_digests,
    system_prompt_pair_digests,
)
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
        event_type = _span_event_type(span, suffix="started")
        data = _span_event_data(span, event_type)
        emit_event(
            event_type,
            source={"component": "integration", "sdk": "openai_agents", "operation": "span.start"},
            data=data,
            status="start",
        )

    def on_span_end(self, span: Any) -> None:
        event_type = _span_event_type(span, suffix="completed")
        data = _span_event_data(span, event_type)
        emit_event(
            event_type,
            source={"component": "integration", "sdk": "openai_agents", "operation": "span.end"},
            data=data,
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


# 区間の中身から、指示の載りうる名前を辞書として取り出す。属性で持つ実装と辞書で持つ実装が
# あるため、収集へ渡す前に形を揃える。
_SPAN_SOURCE_KEYS: tuple[str, ...] = ("input", "instructions", "system_instruction", "messages")


def _holder_kwargs(holder: Any) -> dict[str, Any]:
    if holder is None:
        return {}
    if isinstance(holder, dict):
        return {k: holder[k] for k in _SPAN_SOURCE_KEYS if holder.get(k) is not None}
    out: dict[str, Any] = {}
    for key in _SPAN_SOURCE_KEYS:
        value = getattr(holder, key, None)
        if value is not None:
            out[key] = value
    return out


def _span_event_data(span: Any, event_type: str) -> dict[str, Any]:
    """区間から送出する data を組み立てる。開始と完了で同じ形にする。

    **推論として出す事象は、種別が推論であるだけで判定側が読む入れ物を持たなければならない。**
    判定側は推論の記録を llm の入れ物から読む。入れ物の無い記録は、推論として分類されたのに
    模型も指示も読めない状態になる。

    入れ物は中身の有無で作り分けない。**指示を持たない推論は珍しくない。** 中身があるときだけ
    作る形にすると、その多数派が空の記録として届き、読み手は模型すら取り出せない。推論に
    あたる区間なら常に作り、取り出せた項目だけを入れる。
    """
    data: dict[str, Any] = {
        "agent": {"framework": "openai_agents", "span": _safe_value(span)}
    }
    if not event_type.startswith("llm.request"):
        return data
    llm: dict[str, Any] = {}
    model = _span_model(span)
    if model:
        llm["model"] = model
    line_hashes, pair_hashes = _span_instruction_digests(span)
    if line_hashes:
        llm["system_prompt_line_hashes"] = line_hashes
    if pair_hashes:
        llm["system_prompt_pair_hashes"] = pair_hashes
    data["llm"] = llm
    return data


def _span_model(span: Any) -> str:
    """区間が申告する模型の名前を返す。取れなければ空を返す。"""
    holder = _span_data_of(span)
    for source in (holder, span):
        if source is None:
            continue
        value = source.get("model") if isinstance(source, dict) else getattr(source, "model", None)
        if isinstance(value, str) and value:
            return value
    return ""


def _span_data_of(span: Any) -> Any:
    """区間の中身を返す。実際の枠組みは種別も入力もここへ入れる。"""
    return getattr(span, "span_data", None)


def _span_event_type(span: Any, *, suffix: str) -> str:
    # **種別は区間の中身に載る。** 直下の属性だけを見ると、実際の枠組みが出す推論の区間が
    # 普通の段として扱われ、指示のダイジェストを作る経路へ入らない。
    data = _span_data_of(span)
    span_type = str(
        getattr(span, "type", None)
        or getattr(span, "span_type", None)
        or getattr(data, "type", None)
        or (data.get("type") if isinstance(data, dict) else None)
        or "step"
    ).lower()
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


def _span_instruction_digests(span: Any) -> tuple[list[str], list[str]]:
    """推論区間から、指示にあたる本文の行と語の組のダイジェストを取り出す。

    指示は区間の内容として渡り、呼び出しの引数には現れない。取り出せない形なら空を返す。
    観測の後処理が本来の実行を壊さないよう、例外にしない。

    導出のもとは 1 度だけ組み立てて両方へ渡す。2 度たどると、区間の属性が参照のたびに変わり
    うる実装で行と組が別のもとから作られ、突合が片方だけ成立しない。
    """
    try:
        if "llm.request" not in _span_event_type(span, suffix="completed"):
            return [], []
        value = _safe_value(span)
        holder = _span_data_of(span)
        sources = collect_instruction_sources(value if isinstance(value, dict) else None, None, holder)
        # **役割つきの入力も区間の中身に載る。** 属性としての指示だけを見ると、役割で指示を
        # 渡す形の要求から 1 件も拾えない。中身を辞書として見て同じ収集へ通す。
        sources.extend(collect_instruction_sources(_holder_kwargs(holder)))
        if isinstance(value, dict):
            inner = value.get("span_data")
            if isinstance(inner, dict):
                sources.extend(collect_instruction_sources(inner))
        return system_prompt_line_digests(*sources), system_prompt_pair_digests(*sources)
    except Exception:  # noqa: BLE001
        return [], []
