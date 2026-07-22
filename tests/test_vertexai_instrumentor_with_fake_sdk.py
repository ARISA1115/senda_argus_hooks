import asyncio
import json
import sys
import types
from pathlib import Path

from senda_argus_hooks import register, shutdown


class _UsageMetadata:
    def __init__(self, prompt, candidates):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates


class _FakeResponse:
    def __init__(self, text="hello", usage=None, model_version=None):
        self.text = text
        self.usage_metadata = usage
        if model_version is not None:
            self.model_version = model_version

    def __str__(self):
        return self.text


class _GenerativeModel:
    # 実 SDK と同じく公開メソッドが私有 _generate_content に委譲する構造を再現する
    def __init__(self, model_name, response=None):
        self._model_name = model_name
        self._response = response if response is not None else _FakeResponse()

    def _generate_content(self, contents, **kwargs):
        if kwargs.get("fail"):
            raise RuntimeError("fake vertex failure")
        return self._response

    def generate_content(self, contents, **kwargs):
        return self._generate_content(contents, **kwargs)

    async def _generate_content_async(self, contents, **kwargs):
        return self._response

    async def generate_content_async(self, contents, **kwargs):
        return await self._generate_content_async(contents, **kwargs)


def _install_fake_vertexai(monkeypatch):
    vertexai_mod = types.ModuleType("vertexai")
    generative_mod = types.ModuleType("vertexai.generative_models")
    generative_mod.GenerativeModel = _GenerativeModel
    monkeypatch.setitem(sys.modules, "vertexai", vertexai_mod)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", generative_mod)


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _register(path: Path):
    return register(
        project="test-vertexai",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_ollama=False,
        instrument_bedrock=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        instrument_openai_agents=False,
        capture_prompt=False,
        capture_response=False,
    )


def test_generate_content_emits_llm_request_with_usage_and_swap_response_model(tmp_path, monkeypatch):
    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    result = _register(path)
    assert result["instrumentors"]["vertexai"] is True

    # リクエストは 1.5-pro、応答は 1.5-flash 系を自己申告 = 実際のすり替えのみ送出する
    response = _FakeResponse(usage=_UsageMetadata(12, 7), model_version="gemini-1.5-flash-002")
    model = _GenerativeModel("publishers/google/models/gemini-1.5-pro", response)
    returned = model.generate_content("hello prompt")
    assert returned is response
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["source"]["sdk"] == "vertexai"
    assert llm_event["data"]["llm"]["model"] == "publishers/google/models/gemini-1.5-pro"
    assert llm_event["data"]["llm"]["response_model"] == "gemini-1.5-flash-002"
    assert llm_event["data"]["llm"]["usage"] == {"input_tokens": 12, "output_tokens": 7}
    assert "response_hash" in llm_event["data"]["llm"]["output"]


def test_generate_content_omits_response_model_for_healthy_call(tmp_path, monkeypatch):
    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    # リソースパスと安定版 ID の表記差は偽陽性源のため送出しない
    response = _FakeResponse(model_version="gemini-1.5-pro-002")
    model = _GenerativeModel(
        "projects/p/locations/us-central1/publishers/google/models/gemini-1.5-pro", response
    )
    model.generate_content("hello")
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert "response_model" not in llm_event["data"]["llm"]


def test_generate_content_records_hash_not_plaintext(tmp_path, monkeypatch):
    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    response = _FakeResponse(text="secret model output")
    model = _GenerativeModel("publishers/google/models/gemini-1.5-pro", response)
    model.generate_content("secret prompt text")
    shutdown()

    serialized = path.read_text(encoding="utf-8")
    assert "secret prompt text" not in serialized
    assert "secret model output" not in serialized
    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert "contents_hash" in llm_event["data"]["llm"]["input"]
    assert "response_hash" in llm_event["data"]["llm"]["output"]


def test_generate_content_async_emits_llm_request(tmp_path, monkeypatch):
    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    response = _FakeResponse(usage=_UsageMetadata(5, 3))
    model = _GenerativeModel("publishers/google/models/gemini-1.5-pro", response)
    returned = asyncio.run(model.generate_content_async("async prompt"))
    assert returned is response
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["operation"] == "generate_content"
    assert llm_event["data"]["llm"]["usage"] == {"input_tokens": 5, "output_tokens": 3}


def test_failure_emits_llm_error_and_reraises(tmp_path, monkeypatch):
    import pytest

    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    model = _GenerativeModel("publishers/google/models/gemini-1.5-pro")
    with pytest.raises(RuntimeError, match="fake vertex failure"):
        model.generate_content("prompt", fail=True)
    shutdown()

    events = _read_events(path)
    assert [e["event_type"] for e in events] == ["llm.error"]
    assert events[0]["error"]["type"] == "RuntimeError"


def test_chat_session_private_call_path_is_observed(tmp_path, monkeypatch):
    # ChatSession.send_message は私有 _generate_content を直接呼ぶため、
    # 私有メソッドがパッチ点であることを検証する
    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    model = _GenerativeModel("publishers/google/models/gemini-1.5-pro")
    model._generate_content("chat turn")
    shutdown()

    events = _read_events(path)
    assert [e["event_type"] for e in events] == ["llm.request"]


def test_public_generate_content_emits_single_event(tmp_path, monkeypatch):
    # 公開メソッドは私有メソッドに委譲する。二重計上しないことを検証する
    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    model = _GenerativeModel("publishers/google/models/gemini-1.5-pro")
    model.generate_content("prompt")
    shutdown()

    events = _read_events(path)
    assert len([e for e in events if e["event_type"] == "llm.request"]) == 1


def test_stream_emits_event_after_consumption_with_final_chunk_metadata(tmp_path, monkeypatch):
    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    # usage と自己申告モデルは最終チャンクにしか載らない
    chunks = [
        _FakeResponse(text="chunk-1"),
        _FakeResponse(text="chunk-2", usage=_UsageMetadata(30, 12), model_version="gemini-1.5-flash-002"),
    ]
    model = _GenerativeModel("publishers/google/models/gemini-1.5-pro", chunks)
    stream = model.generate_content("prompt", stream=True)
    consumed = list(stream)
    assert [c.text for c in consumed] == ["chunk-1", "chunk-2"]
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["usage"] == {"input_tokens": 30, "output_tokens": 12}
    assert llm_event["data"]["llm"]["response_model"] == "gemini-1.5-flash-002"
    assert llm_event["data"]["llm"]["stream"] is True
    assert llm_event["data"]["llm"]["chunk_count"] == 2
    assert "response_hash" in llm_event["data"]["llm"]["output"]


def test_stream_abandoned_midway_still_emits_partial_observation(tmp_path, monkeypatch):
    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    chunks = [_FakeResponse(text="chunk-1"), _FakeResponse(text="chunk-2", usage=_UsageMetadata(9, 4))]
    model = _GenerativeModel("publishers/google/models/gemini-1.5-pro", chunks)
    stream = model.generate_content("prompt", stream=True)
    next(stream)
    stream.close()
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["chunk_count"] == 1
    assert "usage" not in llm_event["data"]["llm"]


def test_async_stream_emits_event_after_consumption(tmp_path, monkeypatch):
    _install_fake_vertexai(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)

    chunks = [_FakeResponse(text="c1"), _FakeResponse(text="c2", usage=_UsageMetadata(5, 3))]

    class _AsyncChunks:
        def __init__(self, items):
            self._items = list(items)

        def __aiter__(self):
            async def _iter():
                for item in self._items:
                    yield item

            return _iter()

    model = _GenerativeModel("publishers/google/models/gemini-1.5-pro", _AsyncChunks(chunks))

    async def _consume():
        stream = await model.generate_content_async("prompt", stream=True)
        return [chunk async for chunk in stream]

    consumed = asyncio.run(_consume())
    assert len(consumed) == 2
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["usage"] == {"input_tokens": 5, "output_tokens": 3}
    assert llm_event["data"]["llm"]["chunk_count"] == 2


def test_uninstrument_restores_original(tmp_path, monkeypatch):
    _install_fake_vertexai(monkeypatch)
    original_sync = _GenerativeModel._generate_content
    original_async = _GenerativeModel._generate_content_async
    path = tmp_path / "events.jsonl"
    _register(path)
    assert _GenerativeModel._generate_content is not original_sync
    assert _GenerativeModel._generate_content_async is not original_async
    shutdown()
    assert _GenerativeModel._generate_content is original_sync
    assert _GenerativeModel._generate_content_async is original_async
