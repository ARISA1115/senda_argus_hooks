import io
import json
import sys
import types
from pathlib import Path

from senda_argus_hooks import register, shutdown


class _ServiceModel:
    def __init__(self, service_name):
        self.service_name = service_name


class _Meta:
    def __init__(self, service_name):
        self.service_model = _ServiceModel(service_name)


class _BaseClient:
    def __init__(self, service_name="bedrock-runtime", response=None):
        self.meta = _Meta(service_name)
        self._response = response if response is not None else {}

    def _make_api_call(self, operation_name, api_params):
        if api_params.get("fail"):
            raise RuntimeError("fake bedrock failure")
        return self._response


class _StreamingBody:
    def __init__(self, raw, length):
        self._stream = io.BytesIO(raw)
        self._length = length

    def read(self, *args):
        return self._stream.read(*args)


def _install_fake_botocore(monkeypatch):
    botocore_mod = types.ModuleType("botocore")
    client_mod = types.ModuleType("botocore.client")
    client_mod.BaseClient = _BaseClient
    response_mod = types.ModuleType("botocore.response")
    response_mod.StreamingBody = _StreamingBody
    monkeypatch.setitem(sys.modules, "botocore", botocore_mod)
    monkeypatch.setitem(sys.modules, "botocore.client", client_mod)
    monkeypatch.setitem(sys.modules, "botocore.response", response_mod)


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _register(path: Path):
    return register(
        project="test-bedrock",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_ollama=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        instrument_openai_agents=False,
        capture_prompt=False,
        capture_response=False,
    )


def test_invoke_model_emits_llm_request_with_usage_and_swap_response_model(tmp_path, monkeypatch):
    _install_fake_botocore(monkeypatch)
    path = tmp_path / "events.jsonl"
    result = _register(path)
    assert result["instrumentors"]["bedrock"] is True

    # リクエストは sonnet、応答は haiku を自己申告 = 実際のすり替えのみ送出する
    body = json.dumps({"model": "claude-3-haiku-20240307", "usage": {"input_tokens": 12, "output_tokens": 7}}).encode()
    client = _BaseClient(
        "bedrock-runtime",
        {"body": _StreamingBody(body, len(body)), "ResponseMetadata": {"HTTPHeaders": {}}},
    )
    response = client._make_api_call(
        "InvokeModel", {"modelId": "anthropic.claude-3-sonnet-20240229-v1:0", "body": b"{}"}
    )
    # 観測がボディを消費しても呼び出し元は同内容を再度読める
    assert response["body"].read() == body
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["source"]["sdk"] == "bedrock"
    assert llm_event["data"]["llm"]["model"] == "anthropic.claude-3-sonnet-20240229-v1:0"
    assert llm_event["data"]["llm"]["response_model"] == "claude-3-haiku-20240307"
    assert llm_event["data"]["llm"]["usage"] == {"input_tokens": 12, "output_tokens": 7}
    assert "response_hash" in llm_event["data"]["llm"]["output"]


def test_invoke_model_omits_response_model_for_healthy_anthropic_call(tmp_path, monkeypatch):
    _install_fake_botocore(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    # Bedrock 修飾 ID とネイティブ ID の表記差は偽陽性源のため送出しない
    body = json.dumps({"model": "claude-3-sonnet-20240229"}).encode()
    client = _BaseClient(
        "bedrock-runtime",
        {"body": _StreamingBody(body, len(body)), "ResponseMetadata": {"HTTPHeaders": {}}},
    )
    client._make_api_call(
        "InvokeModel", {"modelId": "us.anthropic.claude-3-sonnet-20240229-v1:0", "body": b"{}"}
    )
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert "response_model" not in llm_event["data"]["llm"]


def test_invoke_model_prefers_header_token_counts(tmp_path, monkeypatch):
    _install_fake_botocore(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    body = b"{}"
    client = _BaseClient(
        "bedrock-runtime",
        {
            "body": _StreamingBody(body, len(body)),
            "ResponseMetadata": {
                "HTTPHeaders": {
                    "x-amzn-bedrock-input-token-count": "120",
                    "x-amzn-bedrock-output-token-count": "45",
                }
            },
        },
    )
    client._make_api_call("InvokeModel", {"modelId": "amazon.titan-text-express-v1", "body": b"{}"})
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["usage"] == {"input_tokens": 120, "output_tokens": 45}


def test_converse_emits_llm_request_with_usage(tmp_path, monkeypatch):
    _install_fake_botocore(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    client = _BaseClient(
        "bedrock-runtime",
        {"output": {"message": {"content": [{"text": "hi"}]}}, "usage": {"inputTokens": 5, "outputTokens": 3}},
    )
    client._make_api_call("Converse", {"modelId": "anthropic.claude-3-sonnet", "messages": []})
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["operation"] == "Converse"
    assert llm_event["data"]["llm"]["usage"] == {"input_tokens": 5, "output_tokens": 3}
    assert "response_hash" in llm_event["data"]["llm"]["output"]


def test_unrelated_operations_and_services_not_observed(tmp_path, monkeypatch):
    _install_fake_botocore(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    _BaseClient("bedrock-runtime", {"ok": True})._make_api_call("ListFoundationModels", {})
    _BaseClient("s3", {"Contents": []})._make_api_call("ListObjectsV2", {"Bucket": "b"})
    shutdown()

    assert _read_events(path) == []


def test_failure_emits_llm_error_and_reraises(tmp_path, monkeypatch):
    import pytest

    _install_fake_botocore(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    client = _BaseClient("bedrock-runtime", {})
    with pytest.raises(RuntimeError, match="fake bedrock failure"):
        client._make_api_call("Converse", {"modelId": "m", "messages": [], "fail": True})
    shutdown()

    events = _read_events(path)
    assert [e["event_type"] for e in events] == ["llm.error"]
    assert events[0]["error"]["type"] == "RuntimeError"


def test_uninstrument_restores_original(tmp_path, monkeypatch):
    _install_fake_botocore(monkeypatch)
    original = _BaseClient._make_api_call
    path = tmp_path / "events.jsonl"
    _register(path)
    assert _BaseClient._make_api_call is not original
    shutdown()
    assert _BaseClient._make_api_call is original
