import json
import sys
import types
from pathlib import Path

import pytest

from senda_argus_hooks import register, shutdown


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _install_fake_openai(monkeypatch):
    class Completions:
        def create(self, *args, **kwargs):
            if kwargs.get("fail"):
                raise RuntimeError("fake openai failure")
            return _Response({"id": "chatcmpl_fake", "model": kwargs.get("model")})

    class Responses:
        def create(self, *args, **kwargs):
            return _Response({"id": "resp_fake", "model": kwargs.get("model")})

    class Embeddings:
        def create(self, *args, **kwargs):
            return _Response({"id": "emb_fake", "model": kwargs.get("model")})

    fake_openai = types.ModuleType("openai")
    fake_openai.resources = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(Completions=Completions)),
        responses=types.SimpleNamespace(Responses=Responses),
        embeddings=types.SimpleNamespace(Embeddings=Embeddings),
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    return fake_openai, Completions


def test_openai_instrumentor_emits_request_error_and_unpatches(tmp_path, monkeypatch):
    _, Completions = _install_fake_openai(monkeypatch)
    original = Completions.create
    path = tmp_path / "events.jsonl"

    result = register(
        project="test-openai",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )
    assert result["instrumentors"]["openai"] is True
    assert Completions.create is not original

    client = Completions()
    assert client.create(model="gpt-fake", messages=[]).model_dump()["model"] == "gpt-fake"
    with pytest.raises(RuntimeError, match="fake openai failure"):
        client.create(model="gpt-fake", messages=[], fail=True)

    shutdown()
    assert Completions.create is original

    events = _read_events(path)
    assert [event["event_type"] for event in events] == ["llm.request", "llm.error"]
    assert events[0]["source"]["sdk"] == "openai"
    assert events[0]["data"]["llm"]["operation"] == "chat.completions.create"
    assert events[0]["data"]["llm"]["model"] == "gpt-fake"
    assert "input_hash" in events[0]["data"]["llm"]["input"]
    assert "response_hash" in events[0]["data"]["llm"]["output"]
    assert events[1]["status"] == "error"
    assert events[1]["error"]["type"] == "RuntimeError"
