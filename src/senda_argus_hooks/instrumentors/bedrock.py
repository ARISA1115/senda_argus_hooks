from __future__ import annotations

import io
import json
import re
import time
from typing import Any, Callable

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.response_meta import extract_response_model as _extract_response_model
from senda_argus_hooks.core.runtime import emit_event, get_config

from .base import BaseInstrumentor


class BedrockInstrumentor(BaseInstrumentor):
    """Instrument Amazon Bedrock via botocore.

    boto3 のクライアントクラスは実行時に動的生成されるため、個別クライアントの
    メソッドではなく botocore.client.BaseClient._make_api_call を単一の割り込み点に
    して bedrock-runtime の InvokeModel / Converse を観測する。

    InvokeModel 応答の StreamingBody は一度しか読めないため、観測のために消費した
    後は同内容のストリームに詰め直して呼び出し元に返す。
    """

    name = "bedrock"

    def __init__(self):
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        try:
            from botocore.client import BaseClient  # type: ignore
        except Exception:
            return False

        if hasattr(BaseClient._make_api_call, "__senda_patched__"):
            return True
        original = BaseClient._make_api_call
        wrapped = self._wrap(original)
        setattr(wrapped, "__senda_patched__", True)
        BaseClient._make_api_call = wrapped  # type: ignore[method-assign]
        self._patches.append((BaseClient, "_make_api_call", original))
        return True

    def _wrap(self, original: Callable) -> Callable:
        def wrapper(client, operation_name, api_params):
            if _service_name(client) != "bedrock-runtime" or operation_name not in ("InvokeModel", "Converse"):
                return original(client, operation_name, api_params)
            cfg = get_config()
            start = time.perf_counter()
            model = str((api_params or {}).get("modelId") or "")
            input_payload = {"params_hash": sha256_value(api_params)} if not cfg.capture_prompt else {"params": api_params}
            try:
                response = original(client, operation_name, api_params)
                latency_ms = int((time.perf_counter() - start) * 1000)
                llm_data: dict[str, Any] = {"provider": "bedrock", "operation": operation_name, "model": model, "input": input_payload}
                if operation_name == "InvokeModel" and isinstance(response, dict):
                    raw = _read_and_rewrap_body(response)
                    parsed = _parse_json_bytes(raw)
                    usage = _invoke_model_usage(response, parsed)
                    if usage:
                        llm_data["usage"] = usage
                    # Bedrock 修飾 ID とネイティブ ID の表記差は偽陽性源のため、
                    # 正規化して同一モデルと判定できる場合は response_model を送出しない。
                    response_model = _extract_response_model(parsed)
                    if response_model and not _models_correspond(model, response_model):
                        llm_data["response_model"] = response_model
                    llm_data["output"] = {"response_hash": sha256_value(raw)} if not cfg.capture_response else {"response": parsed}
                elif operation_name == "Converse" and isinstance(response, dict):
                    usage_raw = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                    usage = _int_usage(usage_raw.get("inputTokens"), usage_raw.get("outputTokens"))
                    if usage:
                        llm_data["usage"] = usage
                    llm_data["output"] = {"response_hash": sha256_value(response.get("output"))} if not cfg.capture_response else {"response": response.get("output")}
                emit_event(
                    "llm.request",
                    source={"component": "instrumentor", "sdk": "bedrock", "provider": "bedrock", "operation": operation_name},
                    data={"llm": llm_data},
                    status="success",
                    latency_ms=latency_ms,
                )
                return response
            except Exception as exc:
                latency_ms = int((time.perf_counter() - start) * 1000)
                emit_event(
                    "llm.error",
                    source={"component": "instrumentor", "sdk": "bedrock", "provider": "bedrock", "operation": operation_name},
                    data={"llm": {"provider": "bedrock", "operation": operation_name, "model": model, "input": input_payload}},
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


def _service_name(client: Any) -> str:
    try:
        return str(client.meta.service_model.service_name)
    except Exception:
        return ""


def _model_core(identifier: str) -> str:
    """Bedrock 修飾 ID とネイティブモデル ID を比較可能な形に正規化する。

    Bedrock の modelId はリージョン接頭辞・プロバイダ接頭辞・バージョン接尾辞を
    含むが、anthropic 系レスポンスのボディはネイティブ ID を自己申告する。
    ドット区切りの最終セグメントからバージョン接尾辞とリビジョンを除去した
    中核部分を比較単位にする。
    """
    core = identifier.strip().lower().split(".")[-1]
    core = core.split(":")[0]
    return re.sub(r"-v\d+$", "", core)


def _models_correspond(model_id: str, response_model: str) -> bool:
    """リクエストの modelId とレスポンス自己申告モデルが同一モデルを指すか判定する。"""
    if not model_id or not response_model:
        return False
    a = _model_core(model_id)
    b = _model_core(response_model)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _read_and_rewrap_body(response: dict[str, Any]) -> bytes | None:
    """StreamingBody を読み取り、呼び出し元が再度読めるよう同内容に詰め直す。"""
    body = response.get("body")
    if body is None or not hasattr(body, "read"):
        return None
    try:
        raw = body.read()
    except Exception:
        return None
    try:
        from botocore.response import StreamingBody  # type: ignore

        response["body"] = StreamingBody(io.BytesIO(raw), len(raw))
    except Exception:
        response["body"] = io.BytesIO(raw)
    return raw


def _parse_json_bytes(raw: bytes | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _int_usage(input_tokens: Any, output_tokens: Any) -> dict[str, int] | None:
    result: dict[str, int] = {}
    try:
        if input_tokens is not None:
            result["input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            result["output_tokens"] = int(output_tokens)
    except (TypeError, ValueError):
        return None
    return result or None


def _invoke_model_usage(response: dict[str, Any], parsed: dict[str, Any]) -> dict[str, int] | None:
    """InvokeModel のトークン使用量を応答ヘッダー優先で抽出し、ボディから補完する。"""
    headers: dict[str, Any] = {}
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("HTTPHeaders"), dict):
        headers = metadata["HTTPHeaders"]
    input_tokens = headers.get("x-amzn-bedrock-input-token-count")
    output_tokens = headers.get("x-amzn-bedrock-output-token-count")
    if input_tokens is None or output_tokens is None:
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            input_tokens = input_tokens if input_tokens is not None else usage.get("input_tokens")
            output_tokens = output_tokens if output_tokens is not None else usage.get("output_tokens")
    if input_tokens is None:
        input_tokens = parsed.get("inputTextTokenCount") or parsed.get("prompt_token_count")
    if output_tokens is None:
        results = parsed.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            output_tokens = results[0].get("tokenCount")
        if output_tokens is None:
            output_tokens = parsed.get("generation_token_count")
    return _int_usage(input_tokens, output_tokens)
