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
            model="claude-resolved-1",
            usage={"input_tokens": 100, "output_tokens": 1, "cache_creation_input_tokens": 7, "cache_read_input_tokens": 20},
        ),
    )
    delta = _Event("message_delta", usage={"output_tokens": 50})
    return [start, _Event("content_block_delta"), delta]


class _Manager:
    def __init__(self, snapshot, fail_enter=False):
        self.stream = types.SimpleNamespace(current_message_snapshot=snapshot)
        self.fail_enter = fail_enter

    def __enter__(self):
        if self.fail_enter:
            raise ConnectionError("request failed")
        return self.stream

    def __exit__(self, *a):
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *a):
        return False


def _install(monkeypatch):
    class Messages:
        # 管理オブジェクトを差し替える試験は、計装が包んだ stream を上書きせず、この工場を差し替える。
        manager_factory = None
        stream_factory = None

        def create(self, *args, **kwargs):
            if kwargs.get("stream"):
                if Messages.stream_factory is not None:
                    return Messages.stream_factory()
                return _Stream(_stream_events())
            return _Response({"id": "m", "model": kwargs.get("model"), "usage": USAGE})

        def stream(self, *args, **kwargs):
            if Messages.manager_factory is not None:
                return Messages.manager_factory()
            snapshot = None if kwargs.get("no_snapshot") else _Response({"model": kwargs.get("model"), "usage": USAGE})
            return _Manager(snapshot, fail_enter=kwargs.get("fail_enter", False))

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


def test_a_user_exception_inside_the_block_still_records_the_usage_already_billed(events_path):
    path, (Messages, _) = events_path
    with pytest.raises(RuntimeError):
        with Messages().stream(model="claude-x", messages=[]):
            raise RuntimeError("user code failed with a secret")
    (event,) = _usage_events(path)
    assert event["data"]["llm"]["usage"] == USAGE
    assert [e["event_type"] for e in _events(path)] == ["llm.request"]


def test_an_exception_with_no_usage_records_only_its_type_not_the_message(events_path):
    path, (Messages, _) = events_path
    with pytest.raises(RuntimeError):
        with Messages().stream(model="claude-x", messages=[], no_snapshot=True):
            raise RuntimeError("user code failed with a secret")
    (event,) = _events(path)
    assert event["event_type"] == "llm.error"
    assert event["error"] == {"type": "RuntimeError", "message": ""}


def test_a_failed_request_on_enter_is_an_error_event_with_its_message(events_path):
    path, (Messages, _) = events_path
    with pytest.raises(ConnectionError):
        with Messages().stream(model="claude-x", messages=[], fail_enter=True):
            pass
    (event,) = _events(path)
    assert event["event_type"] == "llm.error" and event["error"]["message"] == "request failed"


def test_the_stream_true_record_uses_the_model_the_response_reports(events_path):
    path, (Messages, _) = events_path
    list(Messages().create(model="claude-alias", messages=[], stream=True))
    (event,) = _usage_events(path)
    assert event["data"]["llm"]["response_model"] == "claude-resolved-1"
    assert event["data"]["llm"]["model"] == "claude-alias"


def test_the_wrapped_stream_supports_next_and_records_once_at_the_end(events_path):
    path, (Messages, _) = events_path
    stream = Messages().create(model="claude-x", messages=[], stream=True)
    assert next(stream).type == "message_start"
    assert _usage_events(path) == []
    next(stream)
    next(stream)
    with pytest.raises(StopIteration):
        next(stream)
    assert len(_usage_events(path)) == 1
    with pytest.raises(StopIteration):
        next(stream)
    assert len(_usage_events(path)) == 1


def test_a_stream_closed_early_records_the_partial_usage_once(events_path):
    path, (Messages, _) = events_path
    stream = Messages().create(model="claude-x", messages=[], stream=True)
    next(stream)
    stream.close()
    stream.close()
    (event,) = _usage_events(path)
    assert event["data"]["llm"]["usage"]["input_tokens"] == 100


def test_the_async_stream_supports_anext(events_path):
    path, (_, AsyncMessages) = events_path

    async def run():
        stream = await AsyncMessages().create(model="claude-x", messages=[], stream=True)
        first = await stream.__anext__()
        rest = [e async for e in stream]
        return first, rest

    first, rest = asyncio.run(run())
    assert first.type == "message_start" and len(rest) == 2
    assert len(_usage_events(path)) == 1


def test_a_call_through_a_bedrock_or_vertex_client_is_marked_so_it_can_be_left_out(events_path):
    path, (Messages, _) = events_path

    class AnthropicBedrock:
        pass

    resource = Messages()
    resource._client = AnthropicBedrock()
    resource.create(model="claude-x", messages=[])
    Messages().create(model="claude-x", messages=[])
    direct, via = sorted(_usage_events(path), key=lambda e: "via" in e["data"]["llm"])
    assert "via" not in direct["data"]["llm"] and via["data"]["llm"]["via"] == "bedrock"


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


def test_a_snapshot_that_asserts_before_any_event_is_read_means_no_usage(events_path):
    path, (Messages, _) = events_path

    class _AssertingStream:
        @property
        def current_message_snapshot(self):
            raise AssertionError

    class _Mgr(_Manager):
        def __init__(self):
            self.stream = _AssertingStream()
            self.fail_enter = False

    Messages.manager_factory = lambda: _Mgr()
    assert hasattr(type(Messages()).stream, "__senda_patched__")
    with Messages().stream(model="claude-x", messages=[]):
        pass
    # 事象を 1 つも読まずに抜けた。要求は出ているため、量の無い記録を 1 件だけ残し、量は載せない。
    (event,) = _events(path)
    assert event["event_type"] == "llm.request" and "usage" not in event["data"]["llm"]


def test_the_safe_snapshot_accessor_is_preferred_when_the_sdk_has_it(events_path):
    path, (Messages, _) = events_path

    class _Stream:
        @property
        def current_message_snapshot(self):
            raise AssertionError("must not be read")

        def _current_snapshot_or_none(self):
            return _Response({"model": "claude-x", "usage": USAGE})

    class _Mgr(_Manager):
        def __init__(self):
            self.stream = _Stream()
            self.fail_enter = False

    Messages.manager_factory = lambda: _Mgr()
    with Messages().stream(model="claude-x", messages=[]):
        pass
    assert len(_usage_events(path)) == 1


def test_every_non_direct_client_class_of_the_sdk_is_marked(events_path):
    path, (Messages, _) = events_path
    for name in ("AnthropicBedrock", "AsyncAnthropicVertex", "AnthropicFoundry", "AnthropicAWS", "AnthropicGoogleCloud", "AnthropicBedrockMantle"):
        resource = Messages()
        resource._client = type(name, (), {})()
        resource.create(model="claude-x", messages=[])
    resource = Messages()
    resource._client = type("Anthropic", (), {})()
    resource.create(model="claude-x", messages=[])
    marked = [e for e in _usage_events(path) if "via" in e["data"]["llm"]]
    assert len(marked) == 6 and len(_usage_events(path)) == 7


class _ProviderError(Exception):
    """提供元か通信の部品が上げる型。型のモジュールの名前で、提供元の失敗と判定される。"""


_ProviderError.__module__ = "httpx"


def test_a_provider_failure_while_iterating_a_stream_records_partial_usage_and_an_error(events_path):
    path, (Messages, _) = events_path

    class _Breaking(_Stream):
        def __next__(self):
            if not self._events:
                raise _ProviderError("disconnect")
            return self._events.pop(0)

        def __iter__(self):
            return self

    Messages.stream_factory = lambda: _Breaking(_stream_events()[:1])
    stream = Messages().create(model="claude-x", messages=[], stream=True)
    with pytest.raises(_ProviderError):
        list(stream)
    kinds = [e["event_type"] for e in _events(path)]
    assert kinds == ["llm.request", "llm.error"]
    request = _events(path)[0]["data"]["llm"]
    assert request["usage"]["input_tokens"] == 100 and request["usage"]["output_tokens"] == 1


def test_a_failure_before_any_event_is_only_an_error(events_path):
    path, (Messages, _) = events_path

    class _Dead(_Stream):
        def __next__(self):
            raise _ProviderError("connection reset")

        def __iter__(self):
            return self

    Messages.stream_factory = lambda: _Dead([])
    with pytest.raises(_ProviderError):
        list(Messages().create(model="claude-x", messages=[], stream=True))
    assert [e["event_type"] for e in _events(path)] == ["llm.error"]


def test_a_provider_failure_inside_messages_stream_is_an_error_even_with_partial_usage(events_path):
    path, (Messages, _) = events_path
    with pytest.raises(_ProviderError):
        with Messages().stream(model="claude-x", messages=[]):
            raise _ProviderError("disconnect while reading")
    kinds = [e["event_type"] for e in _events(path)]
    assert kinds == ["llm.request", "llm.error"]
    error = _events(path)[1]["error"]
    assert error["type"] == "_ProviderError" and error["message"] == "disconnect while reading"
