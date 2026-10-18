"""OpenAI の計装が、チャットと Responses の量を同じ形で記録し、ストリームと非同期と経由の宛先も扱う。

チャットは prompt_tokens と completion_tokens、Responses は input_tokens と output_tokens で量を返す。どちらも
入力はキャッシュを含む。キャッシュの量は、Anthropic の項目の名前と混ざらないよう cached_input_tokens で載せる。
チャットのストリームは、stream_options の include_usage を要求しない限り量が返らず、その理由を記録に載せる。
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from senda_argus_hooks import register, shutdown
from senda_argus_hooks.instrumentors.openai_stream import observe_openai, usage_of_openai
from senda_argus_hooks.instrumentors.usage_stream import StreamState


def _events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Resp:
    def __init__(self, payload):
        self.payload = payload
        self.usage = payload.get("usage")
        self.model = payload.get("model")

    def model_dump(self):
        return self.payload


CHAT_USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 50,
    "prompt_tokens_details": {"cached_tokens": 800, "audio_tokens": 30},
    "completion_tokens_details": {"audio_tokens": 4},
}
RESP_USAGE = {"input_tokens": 900, "output_tokens": 40, "input_tokens_details": {"cached_tokens": 500}}


class _Ev:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Stream:
    def __init__(self, events):
        self._events = list(events)

    def __iter__(self):
        return iter(list(self._events))

    async def __aiter__(self):
        for e in self._events:
            yield e

    def close(self):
        return None


def _chat_chunks(include_usage):
    chunks = [_Ev(model="gpt-resolved", usage=None, choices=[])]
    if include_usage:
        chunks.append(_Ev(model="gpt-resolved", usage=types.SimpleNamespace(**{k: (types.SimpleNamespace(**v) if isinstance(v, dict) else v) for k, v in CHAT_USAGE.items()}), choices=[]))
    return chunks


def _resp_events():
    resp = types.SimpleNamespace(model="gpt-resolved", usage=types.SimpleNamespace(input_tokens=900, output_tokens=40, input_tokens_details=types.SimpleNamespace(cached_tokens=500)))
    return [_Ev(type="response.created", response=types.SimpleNamespace(model="gpt-resolved", usage=None)), _Ev(type="response.completed", response=resp)]


def _install(monkeypatch):
    class Completions:
        def create(self, *a, **k):
            if k.get("stream"):
                return _Stream(_chat_chunks((k.get("stream_options") or {}).get("include_usage") is True))
            return _Resp({"model": "gpt-resolved", "usage": CHAT_USAGE})

    class AsyncCompletions:
        async def create(self, *a, **k):
            return _Resp({"model": "gpt-resolved", "usage": CHAT_USAGE})

    class Responses:
        def create(self, *a, **k):
            if k.get("stream"):
                return _Stream(_resp_events())
            return _Resp({"model": "gpt-resolved", "usage": RESP_USAGE})

    class AsyncResponses:
        async def create(self, *a, **k):
            return _Resp({"model": "gpt-resolved", "usage": RESP_USAGE})

    class Embeddings:
        def create(self, *a, **k):
            return _Resp({"model": "emb", "usage": {"prompt_tokens": 8, "total_tokens": 8}})

    class AsyncEmbeddings:
        async def create(self, *a, **k):
            return Embeddings().create()

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    module("openai.resources.chat.completions", Completions=Completions, AsyncCompletions=AsyncCompletions)
    module("openai.resources.responses", Responses=Responses, AsyncResponses=AsyncResponses)
    module("openai.resources.embeddings", Embeddings=Embeddings, AsyncEmbeddings=AsyncEmbeddings)
    module("openai.resources.chat")
    module("openai.resources")
    module("openai")
    return Completions, AsyncCompletions, Responses, AsyncResponses, Embeddings


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    classes = _install(monkeypatch)
    register(
        project="openai-usage",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )
    yield path, classes
    shutdown()


def _llm(path):
    return [e["data"]["llm"] for e in _events(path) if e["event_type"] == "llm.request"]


def test_the_chat_usage_keeps_cached_tokens_apart_from_the_input_total():
    out = usage_of_openai(CHAT_USAGE)
    assert out["input_tokens"] == 1000 and out["output_tokens"] == 50
    assert out["cached_input_tokens"] == 800
    assert "cache_read_input_tokens" not in out and "cache_creation_input_tokens" not in out


def test_the_responses_usage_is_read_from_input_and_output_tokens():
    out = usage_of_openai(RESP_USAGE)
    assert out["input_tokens"] == 900 and out["output_tokens"] == 40 and out["cached_input_tokens"] == 500


def test_a_value_that_is_not_an_integer_is_left_out_not_made_zero():
    assert usage_of_openai({"prompt_tokens": "10", "completion_tokens": True}) == {}
    assert usage_of_openai(None) == {}


def test_observe_reads_a_chat_chunk_usage_and_a_responses_completion():
    state = StreamState()
    observe_openai(_Ev(model="m1", usage=None), state)
    assert state.model == "m1" and state.usage == {}
    observe_openai(_chat_chunks(True)[1], state)
    assert state.usage["input_tokens"] == 1000
    state = StreamState()
    for event in _resp_events():
        observe_openai(event, state)
    assert state.usage["output_tokens"] == 40 and state.model == "gpt-resolved"


def test_chat_and_responses_are_recorded_with_the_resolved_model(setup):
    path, (Completions, _, Responses, _, _) = setup
    Completions().create(model="alias", messages=[])
    Responses().create(model="alias", input="x")
    chat, resp = _llm(path)
    assert chat["usage"]["input_tokens"] == 1000 and chat["response_model"] == "gpt-resolved"
    assert resp["usage"] == {"input_tokens": 900, "output_tokens": 40, "cached_input_tokens": 500}


def test_async_calls_are_recorded(setup):
    path, (_, AsyncCompletions, _, AsyncResponses, _) = setup
    asyncio.run(AsyncCompletions().create(model="alias", messages=[]))
    asyncio.run(AsyncResponses().create(model="alias", input="x"))
    assert len(_llm(path)) == 2


def test_a_chat_stream_without_include_usage_is_recorded_without_usage_and_says_why(setup):
    path, (Completions, *_rest) = setup
    list(Completions().create(model="alias", messages=[], stream=True))
    (rec,) = _llm(path)
    assert "usage" not in rec and rec["usage_unavailable"] == "stream_without_include_usage"
    assert rec["response_model"] == "gpt-resolved"


def test_a_chat_stream_with_include_usage_records_the_usage_once(setup):
    path, (Completions, *_rest) = setup
    list(Completions().create(model="alias", messages=[], stream=True, stream_options={"include_usage": True}))
    (rec,) = _llm(path)
    assert rec["usage"]["input_tokens"] == 1000 and "usage_unavailable" not in rec


def test_a_responses_stream_records_the_usage_from_the_completed_event(setup):
    path, (_, _, Responses, *_rest) = setup
    list(Responses().create(model="alias", input="x", stream=True))
    (rec,) = _llm(path)
    assert rec["usage"]["output_tokens"] == 40


class _Breaking:
    """決めた数の事象を返した後で、通信の失敗を上げるストリーム。"""

    def __init__(self, ok):
        self._items = list(ok)

    def __iter__(self):
        return self

    def __next__(self):
        if not self._items:
            raise ConnectionError("reset")
        return self._items.pop(0)


def test_a_failure_before_any_usage_reaches_only_the_error_callback():
    from senda_argus_hooks.instrumentors.usage_stream import UsageStream

    done, errors = [], []
    stream = UsageStream(_Breaking(_resp_events()[:1]), lambda u, m: done.append((u, m)), observe_openai, errors.append)
    next(stream)
    with pytest.raises(ConnectionError):
        next(stream)
    assert len(errors) == 1 and done == []


def test_a_failure_after_usage_was_read_keeps_the_usage_and_also_reports_the_error():
    from senda_argus_hooks.instrumentors.usage_stream import UsageStream

    done, errors = [], []
    stream = UsageStream(_Breaking(_resp_events()), lambda u, m: done.append((u, m)), observe_openai, errors.append)
    next(stream)
    next(stream)
    with pytest.raises(ConnectionError):
        next(stream)
    assert len(errors) == 1 and len(done) == 1 and done[0][0]["output_tokens"] == 40


def test_a_client_for_azure_or_another_host_is_marked_via(setup):
    path, (Completions, *_rest) = setup

    class AzureOpenAI:
        pass

    class Other:
        base_url = types.SimpleNamespace(host="localhost")

    class Direct:
        base_url = types.SimpleNamespace(host="api.openai.com")

    for client in (AzureOpenAI(), Other(), Direct()):
        resource = Completions()
        resource._client = client
        resource.create(model="alias", messages=[])
    vias = [rec.get("via") for rec in _llm(path)]
    assert vias == ["azure", "base_url", None]


def test_unpatching_restores_every_method(tmp_path, monkeypatch):
    Completions, AsyncCompletions, Responses, AsyncResponses, Embeddings = _install(monkeypatch)
    originals = (Completions.create, AsyncCompletions.create, Responses.create, AsyncResponses.create, Embeddings.create)
    register(
        project="openai-usage",
        exporters=[{"type": "jsonl", "path": str(tmp_path / "e.jsonl")}],
        auto_instrument=True,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
    )
    assert Responses.create is not originals[2]
    shutdown()
    assert (Completions.create, AsyncCompletions.create, Responses.create, AsyncResponses.create, Embeddings.create) == originals
