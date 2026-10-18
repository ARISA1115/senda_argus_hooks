"""Anthropic の計装が、キャッシュの内訳と、非同期、stream=True、messages.stream の量を記録に載せる。

プロバイダの使用量との突合が、同じ分け方で比べられるように、input_tokens とは別にキャッシュの読みと
作成のトークンを載せる。ストリームは、読み終えたとき、または with を抜けるときに 1 件だけ記録する。
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from senda_argus_hooks import register, shutdown


def _events(path: Path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.usage = payload.get("usage")
        self.model = payload.get("model")

    def model_dump(self):
        return self.payload


USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_creation_input_tokens": 7,
    "cache_read_input_tokens": 20,
}


class _Event:
    def __init__(self, type_, **kw):
        self.type = type_
        self.__dict__.update(kw)


class _Stream:
    def __init__(self, events):
        self._events = events

    def __iter__(self):
        return iter(self._events)

    async def __aiter__(self):
        for e in self._events:
            yield e

    def close(self):
        return None


def _stream_events():
    start = _Event(
        "message_start",
        message=types.SimpleNamespace(
            usage={
                "input_tokens": 100,
                "output_tokens": 1,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 20,
            }
        ),
    )
    delta = _Event("message_delta", usage={"output_tokens": 50})
    return [start, _Event("content_block_delta"), delta]


class _Manager:
    def __init__(self, snapshot):
        self.stream = types.SimpleNamespace(current_message_snapshot=snapshot)

    def __enter__(self):
        return self.stream

    def __exit__(self, *a):
        return False

    async def __aenter__(self):
        return self.stream

    async def __aexit__(self, *a):
        return False


def _install(monkeypatch):
    class Messages:
        def create(self, *args, **kwargs):
            if kwargs.get("stream"):
                return _Stream(_stream_events())
            return _Response({"id": "m", "model": kwargs.get("model"), "usage": USAGE})

        def stream(self, *args, **kwargs):
            return _Manager(_Response({"model": kwargs.get("model"), "usage": USAGE}))

    class AsyncMessages:
        async def create(self, *args, **kwargs):
            if kwargs.get("stream"):
                return _Stream(_stream_events())
            return _Response({"id": "m", "model": kwargs.get("model"), "usage": USAGE})

        def stream(self, *args, **kwargs):
            return _Manager(_Response({"model": kwargs.get("model"), "usage": USAGE}))

    fake = types.ModuleType("anthropic")
    fake.resources = types.SimpleNamespace(
        messages=types.SimpleNamespace(Messages=Messages, AsyncMessages=AsyncMessages)
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return Messages, AsyncMessages


@pytest.fixture
def events_path(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    classes = _install(monkeypatch)
    register(
        project="usage-test",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )
    yield path, classes
    shutdown()


def _usage_events(path):
    return [e for e in _events(path) if e["event_type"] == "llm.request"]


def test_the_cache_tokens_are_recorded_beside_input_tokens(events_path):
    path, (Messages, _) = events_path
    Messages().create(model="claude-x", messages=[])
    (event,) = _usage_events(path)
    assert event["data"]["llm"]["usage"] == USAGE


def test_an_async_create_is_recorded(events_path):
    path, (_, AsyncMessages) = events_path
    asyncio.run(AsyncMessages().create(model="claude-x", messages=[]))
    (event,) = _usage_events(path)
    assert event["data"]["llm"]["usage"] == USAGE
    assert event["data"]["llm"]["operation"] == "messages.create"


def test_a_stream_true_call_records_once_after_the_events_are_read(events_path):
    path, (Messages, _) = events_path
    stream = Messages().create(model="claude-x", messages=[], stream=True)
    assert _usage_events(path) == []
    assert len(list(stream)) == 3
    (event,) = _usage_events(path)
    assert event["data"]["llm"]["usage"] == USAGE


def test_an_async_stream_true_call_records_once(events_path):
    path, (_, AsyncMessages) = events_path

    async def run():
        stream = await AsyncMessages().create(
            model="claude-x", messages=[], stream=True
        )
        return [e async for e in stream]

    assert len(asyncio.run(run())) == 3
    (event,) = _usage_events(path)
    assert event["data"]["llm"]["usage"] == USAGE


def test_messages_stream_records_when_the_with_block_exits(events_path):
    path, (Messages, _) = events_path
    with Messages().stream(model="claude-x", messages=[]):
        assert _usage_events(path) == []
    (event,) = _usage_events(path)
    assert event["data"]["llm"]["operation"] == "messages.stream"
    assert event["data"]["llm"]["usage"] == USAGE


def test_an_async_messages_stream_records_on_exit(events_path):
    path, (_, AsyncMessages) = events_path

    async def run():
        async with AsyncMessages().stream(model="claude-x", messages=[]):
            pass

    asyncio.run(run())
    assert len(_usage_events(path)) == 1


def test_an_error_inside_the_stream_block_is_an_error_event_not_a_usage_event(
    events_path,
):
    path, (Messages, _) = events_path
    with pytest.raises(RuntimeError):
        with Messages().stream(model="claude-x", messages=[]):
            raise RuntimeError("api down")
    assert _usage_events(path) == []
    assert [e["event_type"] for e in _events(path)] == ["llm.error"]


def test_unpatching_restores_every_patched_method(tmp_path, monkeypatch):
    Messages, AsyncMessages = _install(monkeypatch)
    originals = (
        Messages.create,
        Messages.stream,
        AsyncMessages.create,
        AsyncMessages.stream,
    )
    register(
        project="usage-test",
        exporters=[{"type": "jsonl", "path": str(tmp_path / "e.jsonl")}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
    )
    assert (
        Messages.create is not originals[0] and AsyncMessages.stream is not originals[3]
    )
    shutdown()
    assert (
        Messages.create,
        Messages.stream,
        AsyncMessages.create,
        AsyncMessages.stream,
    ) == originals
