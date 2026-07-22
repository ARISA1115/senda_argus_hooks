from __future__ import annotations

import json
import time
from typing import Any, Callable

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.identity import data_source_hash, derive_mcp_profile_id, derive_purpose_id, mcp_data_source_profile, normalize_url
from senda_argus_hooks.core.runtime import emit_event, get_config
from senda_argus_hooks.core.purpose_registry import register_mcp_tool_source, selected_tool_purpose
from .base import BaseInstrumentor


class ArgusSDKInstrumentor(BaseInstrumentor):
    name = "argus_sdk"

    def __init__(self):
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        patched = False
        try:
            from senda_argus_hooks.sdk import MockLLMClient, OllamaClient, MockMCPClient, PromptOpsClient
            candidates = [
                (MockLLMClient, "generate_answer", "llm", "mock.generate_answer"),
                (MockLLMClient, "refine_prompt", "llm", "mock.refine_prompt"),
                (OllamaClient, "chat", "llm", "ollama.chat"),
                (MockMCPClient, "call_tool", "mcp", "mcp.call_tool"),
                (PromptOpsClient, "agent_decision", "promptops", "agent.decision"),
                (PromptOpsClient, "run_completed", "promptops", "promptops.run.completed"),
            ]
        except Exception:
            candidates = []
        for cls, method_name, kind, operation in candidates:
            original = getattr(cls, method_name, None)
            if original is None or hasattr(original, "__senda_patched__"):
                continue
            wrapped = self._wrap_llm(original, operation) if kind == "llm" else (self._wrap_mcp(original, operation) if kind == "mcp" else self._wrap_promptops(original, operation))
            setattr(wrapped, "__senda_patched__", True)
            setattr(cls, method_name, wrapped)
            self._patches.append((cls, method_name, original))
            patched = True
        return patched

    def _wrap_llm(self, original: Callable, operation: str) -> Callable:
        def wrapper(obj, *args, **kwargs):
            cfg = get_config()
            start = time.perf_counter()
            input_payload = _input_payload(args, kwargs, cfg.capture_prompt, cfg.capture_hash)
            provider = "ollama" if operation.startswith("ollama") else "mock"
            model = kwargs.get("model") or getattr(obj, "model", provider)
            purpose = kwargs.get("purpose") or _purpose_from_args(args) or ("refiner" if "refine" in operation else "answer")
            messages_hash = sha256_value(args[0]) if args else None
            try:
                response = original(obj, *args, **kwargs)
                latency_ms = int((time.perf_counter() - start) * 1000)
                output_payload = _output_payload(response, cfg.capture_response, cfg.capture_hash)
                # レスポンスが自己申告した実モデルは model を上書きせず response_model として
                # 別フィールドで保持する。上書きするとリクエスト時に指定したモデルの情報が
                # 失われ、Argus 側の ModelSwapRule がすり替えを検知できなくなる。
                response_model = None
                if isinstance(response, dict):
                    response_model = response.get("model")
                    purpose = response.get("purpose") or purpose
                    report, actual_tools = _extract_senda_argus_report(response)
                    if report is not None:
                        steering_detected = bool(actual_tools) and report.get("tool_name") not in {
                            (tc.get("function") or {}).get("name") for tc in actual_tools if isinstance(tc, dict)
                        }
                        emit_event(
                            "llm.tool_selection.proposed",
                            source={"component": "instrumentor", "sdk": "senda_argus_hooks.sdk", "provider": provider, "operation": operation},
                            data={"senda_argus_report": report, "steering_detected": steering_detected},
                            status="success",
                        )
                llm_data = {"provider": provider, "operation": operation, "purpose": purpose, "model": model, "input": input_payload, "output": output_payload}
                if messages_hash:
                    llm_data["messages_hash"] = messages_hash
                usage = _extract_usage(response)
                if usage:
                    llm_data["usage"] = usage
                if isinstance(response_model, str) and response_model.strip():
                    llm_data["response_model"] = response_model
                emit_event(
                    "llm.request",
                    source={"component": "instrumentor", "sdk": "senda_argus_hooks.sdk", "provider": provider, "operation": operation},
                    data={"llm": llm_data},
                    status="success",
                    latency_ms=latency_ms,
                )
                return response
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event(
                    "llm.error",
                    source={"component": "instrumentor", "sdk": "senda_argus_hooks.sdk", "provider": provider, "operation": operation},
                    data={"llm": {"provider": provider, "operation": operation, "purpose": purpose, "model": model, "input": input_payload}},
                    status="error",
                    latency_ms=latency_ms,
                    error={"type": exc.__class__.__name__, "message": str(exc)},
                )
                raise
        return wrapper

    def _wrap_mcp(self, original: Callable, operation: str) -> Callable:
        def wrapper(obj, *args, **kwargs):
            cfg = get_config()
            start = time.perf_counter()
            tool = args[0] if args else kwargs.get("tool") or kwargs.get("name")
            arguments = args[1] if len(args) > 1 else kwargs.get("arguments") or {}
            capability = kwargs.get("capability")
            server = getattr(obj, "server", "unknown")
            server_url = getattr(obj, "url", None) or getattr(obj, "base_url", None) or getattr(obj, "server_url", None)
            purpose_profile = mcp_data_source_profile(mcp_server_name=server, mcp_server_url=server_url, tool_name=tool, capability=capability)
            purpose_id = derive_purpose_id(mcp_server_name=server, mcp_server_url=server_url, tool_name=tool, capability=capability)
            mcp_profile_id = derive_mcp_profile_id(mcp_server_name=server, mcp_server_url=server_url, tools=list(getattr(obj, "tools", {}).keys()) if hasattr(obj, "tools") else [])
            register_mcp_tool_source(
                tool_name=tool,
                mcp_server_name=server,
                mcp_server_url=server_url,
                capability=capability,
            )
            raw_args_payload = {"tool": tool, "arguments": arguments, "capability": capability}
            args_payload = {**raw_args_payload, "purpose_id": purpose_id, "data_source_hash": data_source_hash(purpose_profile)}
            base_mcp = {
                "server": server,
                "server_url": normalize_url(str(server_url)) if server_url else None,
                "operation": operation,
                "tool": tool,
                "capability": capability,
                "purpose_id": purpose_id,
                "purpose_source": "mcp_data_source_hash",
                "purpose_profile": purpose_profile,
                "data_source_hash": data_source_hash(purpose_profile),
                "mcp_profile_id": mcp_profile_id,
                "arguments_hash": sha256_value(raw_args_payload),
            }
            if cfg.capture_arguments:
                base_mcp["arguments"] = args_payload
            emit_event(
                "mcp.tool_call.requested",
                source={"component": "instrumentor", "sdk": "senda_argus_hooks.sdk", "operation": operation},
                data={"mcp": base_mcp},
                status="start",
                purpose_id=purpose_id,
            )
            try:
                response = original(obj, *args, **kwargs)
                latency_ms = int((time.perf_counter() - start) * 1000)
                raw_result = _safe_response(response)
                result_payload = raw_result if cfg.capture_result else None
                completed_mcp = dict(base_mcp)
                if result_payload is not None:
                    completed_mcp["result"] = result_payload
                completed_mcp["result_hash"] = sha256_value(raw_result)
                emit_event(
                    "mcp.tool_call.completed",
                    source={"component": "instrumentor", "sdk": "senda_argus_hooks.sdk", "operation": operation},
                    data={"mcp": completed_mcp},
                    status="success",
                    latency_ms=latency_ms,
                    purpose_id=purpose_id,
                )
                return response
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event(
                    "mcp.tool_call.failed",
                    source={"component": "instrumentor", "sdk": "senda_argus_hooks.sdk", "operation": operation},
                    data={"mcp": base_mcp},
                    status="error",
                    latency_ms=latency_ms,
                    error={"type": exc.__class__.__name__, "message": str(exc)},
                    purpose_id=purpose_id,
                )
                raise
        return wrapper


    def _wrap_promptops(self, original: Callable, operation: str) -> Callable:
        def wrapper(obj, *args, **kwargs):
            start = time.perf_counter()
            try:
                response = original(obj, *args, **kwargs)
                latency_ms = int((time.perf_counter() - start) * 1000)
                event_type = operation
                source = {"component": "instrumentor", "sdk": "senda_argus_hooks.sdk", "operation": operation}
                data = dict(response) if isinstance(response, dict) else {"result": response}
                if event_type == "agent.decision" and isinstance(data, dict):
                    selected_tool = data.get("selected_tool")
                    purpose_meta = selected_tool_purpose(
                        selected_tool,
                        default_capability=data.get("capability") or data.get("selected_tool_capability") or "security_intelligence",
                    )
                    if purpose_meta:
                        for key, value in purpose_meta.items():
                            data.setdefault(key, value)
                emit_event(event_type, source=source, data=data, status="success", latency_ms=latency_ms)
                return response
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event("promptops.error", source={"component": "instrumentor", "sdk": "senda_argus_hooks.sdk", "operation": operation}, data={"args": args, "kwargs": kwargs}, status="error", latency_ms=latency_ms, error={"type": exc.__class__.__name__, "message": str(exc)})
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



def _output_payload(response: Any, capture: bool, capture_hash: bool = True) -> dict[str, Any]:
    safe = _safe_response(response)
    metadata = _llm_output_hash_metadata(safe) if capture_hash else {}
    if capture:
        if isinstance(safe, dict):
            return {**safe, **metadata}
        return {"response": safe, **metadata}
    payload = {"response_hash": sha256_value(safe), **metadata}
    structured = _extract_senda_argus_content(safe)
    if structured is not None:
        payload["senda_argus_content"] = structured
        payload["senda_argus_content_hash"] = sha256_value(structured)
    return payload


def _llm_output_hash_metadata(safe_response: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    content = _extract_message_content(safe_response)
    if content is not None:
        metadata["message_content_hash"] = sha256_value(content)
        if isinstance(content, str):
            metadata["message_content_length"] = len(content)
    return metadata


def _extract_message_content(safe_response: Any) -> Any:
    if isinstance(safe_response, dict):
        message = safe_response.get("message")
        if isinstance(message, dict) and "content" in message:
            return message.get("content")
        choices = safe_response.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict) and "content" in msg:
                return msg.get("content")
        if "content" in safe_response:
            return safe_response.get("content")
    return None


def _extract_senda_argus_content(safe_response: Any) -> dict[str, Any] | None:
    content = _extract_message_content(safe_response)
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    sac = parsed.get("senda_argus_content")
    if not isinstance(sac, dict):
        return None
    selected_tool = sac.get("selected_tool")
    safe_selected_tool = None
    if isinstance(selected_tool, dict):
        safe_selected_tool = {
            "name": selected_tool.get("name"),
            "arguments_hash": sha256_value(selected_tool.get("arguments") or {}),
        }
        if isinstance(selected_tool.get("arguments"), dict):
            safe_selected_tool["arguments_count"] = len(selected_tool.get("arguments", {}))
    safe_content = {
        "task_summary": sac.get("task_summary"),
        "reason_summary": sac.get("reason_summary"),
        "selected_tool": safe_selected_tool,
        "planned_tool_call_count": sac.get("planned_tool_call_count"),
        "uncertainty": sac.get("uncertainty"),
        "missing_information_count": len(sac.get("missing_information") or []) if isinstance(sac.get("missing_information"), list) else None,
    }
    return {k: v for k, v in safe_content.items() if v is not None}

def _purpose_from_args(args) -> str | None:
    if args and isinstance(args[0], dict):
        return args[0].get("purpose")
    return None


def _extract_senda_argus_report(response: dict) -> tuple[dict | None, list]:
    """tool_calls から senda_argus_report を取り出し、残りの実ツール呼び出しを返す。

    senda_argus_report エントリは response["message"]["tool_calls"] から除去される。
    """
    msg = response.get("message")
    if not isinstance(msg, dict):
        return None, []
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None, []
    report_args: dict | None = None
    actual_tools: list = []
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if isinstance(fn, dict) and fn.get("name") == "senda_argus_report":
            raw_args = fn.get("arguments") or {}
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {}
            report_args = raw_args
        else:
            actual_tools.append(tc)
    if report_args is not None:
        msg["tool_calls"] = actual_tools
    return report_args, actual_tools


def _safe_response(response: Any) -> Any:
    for attr in ("model_dump", "dict", "json"):
        if hasattr(response, attr):
            try:
                return getattr(response, attr)()
            except Exception:
                pass
    return response


def _extract_usage(response: Any) -> dict[str, int] | None:
    """MockLLMClient/OllamaClient レスポンスからトークン使用量を抽出する。

    Ollama の /api/chat は prompt_eval_count/eval_count をトップレベルで返す。
    OpenAI 互換 usage 形状 (prompt_tokens/completion_tokens) も併せて許容する。
    """
    if not isinstance(response, dict):
        return None
    usage = response.get("usage")
    input_tokens = (usage or {}).get("prompt_tokens") if isinstance(usage, dict) else None
    output_tokens = (usage or {}).get("completion_tokens") if isinstance(usage, dict) else None
    if input_tokens is None:
        input_tokens = response.get("prompt_eval_count")
    if output_tokens is None:
        output_tokens = response.get("eval_count")
    result: dict[str, int] = {}
    if input_tokens is not None:
        result["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        result["output_tokens"] = int(output_tokens)
    return result or None
