from __future__ import annotations

import time
from typing import Any

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.identity import derive_tool_purpose_id
from senda_argus_hooks.core.runtime import emit_event, get_config

try:  # Optional dependency. Unit tests use this module without LangChain installed.
    from langchain_core.callbacks import BaseCallbackHandler as _BaseCallbackHandler
except Exception:  # pragma: no cover - depends on optional dependency availability
    _BaseCallbackHandler = object


class SendaArgusCallbackHandler(_BaseCallbackHandler):
    """LangChain callback handler that emits normalized Senda-Argus events.

    The handler is intentionally optional and does not make LangChain a hard
    dependency. Add it to LangChain callback configuration when you want to
    capture framework-level events without application-level audit calls.
    """

    def __init__(self, *, framework: str = "langchain", capture_payloads: bool | None = None):
        try:
            super().__init__()
        except TypeError:
            pass
        self.framework = framework
        self.capture_payloads = capture_payloads
        self._starts: dict[str, float] = {}
        self._messages_hashes: dict[str, str] = {}
        self._tool_types: dict[str, str] = {}
        self._requested_models: dict[str, str] = {}

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any) -> None:
        run_id = _run_key(kwargs)
        self._starts[run_id] = time.perf_counter()
        self._messages_hashes[run_id] = sha256_value(prompts)
        requested_model = _requested_model(serialized, kwargs)
        if requested_model:
            self._requested_models[run_id] = requested_model
        payload = {"serialized": serialized, "prompts": prompts, "kwargs": _safe_kwargs(kwargs)}
        emit_event(
            "llm.request.started",
            source={"component": "integration", "sdk": self.framework, "operation": "on_llm_start"},
            data={"llm": _payload_or_hash("input", payload, self._capture_prompt())},
            status="start",
        )

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list[Any], **kwargs: Any) -> None:
        run_id = _run_key(kwargs)
        self._starts[run_id] = time.perf_counter()
        self._messages_hashes[run_id] = sha256_value(messages)
        requested_model = _requested_model(serialized, kwargs)
        if requested_model:
            self._requested_models[run_id] = requested_model
        payload = {"serialized": serialized, "messages": messages, "kwargs": _safe_kwargs(kwargs)}
        emit_event(
            "llm.request.started",
            source={"component": "integration", "sdk": self.framework, "operation": "on_chat_model_start"},
            data={"llm": _payload_or_hash("input", payload, self._capture_prompt())},
            status="start",
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        run_id = _run_key(kwargs)
        latency_ms = _latency_ms(self._starts.pop(run_id, None))
        messages_hash = self._messages_hashes.pop(run_id, None)
        requested_model = self._requested_models.pop(run_id, None)
        usage = _extract_llm_usage(response)
        payload = _safe_value(response)
        llm_data = _payload_or_hash("output", payload, self._capture_response())
        if messages_hash:
            llm_data["messages_hash"] = messages_hash
        if usage:
            llm_data["usage"] = usage
        # ModelSwapRule 用: リクエスト時に指定したモデルと、レスポンスが自己申告した
        # 実モデルの両方を送出し、Argus 側でモデルすり替えを検知できるようにする。
        if requested_model:
            llm_data["model"] = requested_model
        response_model = _extract_response_model(response)
        if response_model:
            llm_data["response_model"] = response_model
        emit_event(
            "llm.request",
            source={"component": "integration", "sdk": self.framework, "operation": "on_llm_end"},
            data={"llm": llm_data},
            status="success",
            latency_ms=latency_ms,
        )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        latency_ms = _latency_ms(self._starts.pop(_run_key(kwargs), None))
        emit_event(
            "llm.error",
            source={"component": "integration", "sdk": self.framework, "operation": "on_llm_error"},
            data={"llm": {"run_id": str(kwargs.get("run_id")) if kwargs.get("run_id") else None}},
            status="error",
            latency_ms=latency_ms,
            error={"type": error.__class__.__name__, "message": str(error)},
        )

    def on_tool_start(self, serialized: dict[str, Any], input_str: str | dict[str, Any] | None = None, **kwargs: Any) -> None:
        run_id = _run_key(kwargs)
        self._starts[run_id] = time.perf_counter()
        tool_name = _tool_name(serialized, kwargs)
        tool_type = _tool_type(serialized, kwargs)
        self._tool_types[run_id] = tool_type
        purpose_id = derive_tool_purpose_id(framework=self.framework, tool_name=tool_name, tool_type=tool_type)
        tool = {
            "framework": self.framework,
            "tool_name": tool_name,
            "tool_type": tool_type,
            "purpose_id": purpose_id,
            "arguments_hash": sha256_value(input_str),
        }
        if get_config().capture_arguments:
            tool["arguments"] = input_str
        emit_event(
            "tool_call.requested",
            source={"component": "integration", "sdk": self.framework, "operation": "on_tool_start"},
            data={"tool": tool},
            status="start",
            purpose_id=purpose_id,
        )

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        run_id = _run_key(kwargs)
        latency_ms = _latency_ms(self._starts.pop(run_id, None))
        tool_type = self._tool_types.pop(run_id, "function")
        tool_name = str(kwargs.get("name") or kwargs.get("tool") or "unknown")
        purpose_id = derive_tool_purpose_id(framework=self.framework, tool_name=tool_name, tool_type=tool_type)
        tool = {
            "framework": self.framework,
            "tool_name": tool_name,
            "tool_type": tool_type,
            "purpose_id": purpose_id,
            "result_hash": sha256_value(output),
        }
        if get_config().capture_result:
            tool["result"] = _safe_value(output)
        emit_event(
            "tool_call.completed",
            source={"component": "integration", "sdk": self.framework, "operation": "on_tool_end"},
            data={"tool": tool},
            status="success",
            latency_ms=latency_ms,
            purpose_id=purpose_id,
        )

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        run_id = _run_key(kwargs)
        latency_ms = _latency_ms(self._starts.pop(run_id, None))
        tool_type = self._tool_types.pop(run_id, "function")
        tool_name = str(kwargs.get("name") or kwargs.get("tool") or "unknown")
        purpose_id = derive_tool_purpose_id(framework=self.framework, tool_name=tool_name, tool_type=tool_type)
        emit_event(
            "tool_call.failed",
            source={"component": "integration", "sdk": self.framework, "operation": "on_tool_error"},
            data={"tool": {"framework": self.framework, "tool_name": tool_name, "tool_type": tool_type, "purpose_id": purpose_id}},
            status="error",
            latency_ms=latency_ms,
            error={"type": error.__class__.__name__, "message": str(error)},
            purpose_id=purpose_id,
        )

    def on_chain_start(self, serialized: dict[str, Any], inputs: dict[str, Any] | None = None, **kwargs: Any) -> None:
        run_id = _run_key(kwargs)
        self._starts[run_id] = time.perf_counter()
        emit_event(
            "agent.step.started",
            source={"component": "integration", "sdk": self.framework, "operation": "on_chain_start"},
            data={"agent": {"step_type": "chain", "name": _serialized_name(serialized), "input_hash": sha256_value(inputs)}},
            status="start",
        )

    def on_chain_end(self, outputs: dict[str, Any] | None = None, **kwargs: Any) -> None:
        latency_ms = _latency_ms(self._starts.pop(_run_key(kwargs), None))
        emit_event(
            "agent.step.completed",
            source={"component": "integration", "sdk": self.framework, "operation": "on_chain_end"},
            data={"agent": {"step_type": "chain", "output_hash": sha256_value(outputs)}},
            status="success",
            latency_ms=latency_ms,
        )

    def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
        latency_ms = _latency_ms(self._starts.pop(_run_key(kwargs), None))
        emit_event(
            "agent.step.failed",
            source={"component": "integration", "sdk": self.framework, "operation": "on_chain_error"},
            data={"agent": {"step_type": "chain"}},
            status="error",
            latency_ms=latency_ms,
            error={"type": error.__class__.__name__, "message": str(error)},
        )

    def on_agent_action(self, action: Any, **kwargs: Any) -> None:
        tool_name = getattr(action, "tool", None)
        if tool_name is None and isinstance(action, dict):
            tool_name = action.get("tool")
        # on_tool_start/on_tool_end/on_tool_error と同じ derive_tool_purpose_id を使い、
        # 同一 tool_name であれば宣言時と実行時で purpose_id が一致するようにする。
        # IntentExecutionMismatchRule 相当の突合を LangChain 経路でも成立させるための値。
        # tool_type は AgentAction から取得できないため _tool_type() の既定値 "function" に揃える。
        purpose_id = derive_tool_purpose_id(framework=self.framework, tool_name=tool_name, tool_type="function") if tool_name else None
        data = {"agent": {"action": _safe_value(action), "kwargs": _safe_kwargs(kwargs)}}
        if purpose_id:
            data["selected_tool_purpose_id"] = purpose_id
        emit_event(
            "agent.decision",
            source={"component": "integration", "sdk": self.framework, "operation": "on_agent_action"},
            data=data,
            status="success",
        )

    def on_agent_finish(self, finish: Any, **kwargs: Any) -> None:
        emit_event(
            "agent.run.completed",
            source={"component": "integration", "sdk": self.framework, "operation": "on_agent_finish"},
            data={"agent": {"finish": _safe_value(finish), "kwargs": _safe_kwargs(kwargs)}},
            status="success",
        )

    def _capture_prompt(self) -> bool:
        return get_config().capture_prompt if self.capture_payloads is None else self.capture_payloads

    def _capture_response(self) -> bool:
        return get_config().capture_response if self.capture_payloads is None else self.capture_payloads


def _payload_or_hash(key: str, value: Any, capture: bool) -> dict[str, Any]:
    return {key: _safe_value(value)} if capture else {f"{key}_hash": sha256_value(value)}


def _extract_llm_usage(response: Any) -> dict[str, int] | None:
    llm_output = getattr(response, "llm_output", None)
    if isinstance(response, dict) and llm_output is None:
        llm_output = response.get("llm_output")
    if not isinstance(llm_output, dict):
        return None
    usage = llm_output.get("token_usage") or llm_output.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
    output_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
    result: dict[str, int] = {}
    if input_tokens is not None:
        result["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        result["output_tokens"] = int(output_tokens)
    return result or None


def _requested_model(serialized: dict[str, Any] | None, kwargs: dict[str, Any]) -> str | None:
    """リクエスト時に指定されたモデル識別子を抽出する。

    LangChain の callback は invocation_params (実行時パラメータ) に model /
    model_name を載せる。無い場合は serialized の kwargs から取得する。
    """
    invocation_params = kwargs.get("invocation_params")
    if isinstance(invocation_params, dict):
        model = invocation_params.get("model") or invocation_params.get("model_name")
        if isinstance(model, str) and model.strip():
            return model
    serialized_kwargs = (serialized or {}).get("kwargs")
    if isinstance(serialized_kwargs, dict):
        model = serialized_kwargs.get("model") or serialized_kwargs.get("model_name")
        if isinstance(model, str) and model.strip():
            return model
    return None


def _extract_response_model(response: Any) -> str | None:
    """LLMResult がレスポンス側で自己申告した実モデル識別子を抽出する。

    LangChain の LLMResult は llm_output に model_name (OpenAI 系) または
    model を載せる。取得できない場合は None。
    """
    llm_output = getattr(response, "llm_output", None)
    if llm_output is None and isinstance(response, dict):
        llm_output = response.get("llm_output")
    if not isinstance(llm_output, dict):
        return None
    model = llm_output.get("model_name") or llm_output.get("model")
    if isinstance(model, str) and model.strip():
        return model
    return None


def _run_key(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("run_id") or kwargs.get("parent_run_id") or "default")


def _latency_ms(start: float | None) -> int | None:
    return int((time.perf_counter() - start) * 1000) if start is not None else None


def _safe_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: str(v) if k.endswith("_id") else _safe_value(v) for k, v in kwargs.items() if k not in {"callbacks", "manager"}}


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


def _serialized_name(serialized: dict[str, Any] | None) -> str | None:
    if not serialized:
        return None
    return serialized.get("name") or serialized.get("id") or serialized.get("repr")


def _tool_name(serialized: dict[str, Any] | None, kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("name") or kwargs.get("tool") or _serialized_name(serialized) or "unknown")


def _tool_type(serialized: dict[str, Any] | None, kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("tool_type") or (serialized or {}).get("tool_type") or "function")
