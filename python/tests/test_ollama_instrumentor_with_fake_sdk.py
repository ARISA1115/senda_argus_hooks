from __future__ import annotations

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


def _install_fake_ollama(monkeypatch):
    def chat(*args, **kwargs):
        if kwargs.get("fail"):
            raise RuntimeError("fake ollama failure")
        return _Response({"message": {"content": "module ok"}, "model": kwargs.get("model")})

    class Client:
        def chat(self, *args, **kwargs):
            if kwargs.get("fail"):
                raise RuntimeError("fake ollama client failure")
            return _Response({"message": {"content": "client ok"}, "model": kwargs.get("model")})

    fake_ollama = types.ModuleType("ollama")
    fake_ollama.chat = chat
    fake_ollama.Client = Client
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)
    return fake_ollama, Client


def test_ollama_module_chat_emits_request_error_and_unpatches(tmp_path, monkeypatch):
    fake_ollama, _ = _install_fake_ollama(monkeypatch)
    original = fake_ollama.chat
    path = tmp_path / "events.jsonl"

    result = register(
        project="test-ollama",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        instrument_openai_agents=False,
        capture_prompt=False,
        capture_response=False,
    )
    assert result["instrumentors"]["ollama"] is True
    assert fake_ollama.chat is not original

    assert fake_ollama.chat(model="argus-qwen25-14b-toolplan:latest", messages=[]).model_dump()["model"] == "argus-qwen25-14b-toolplan:latest"
    with pytest.raises(RuntimeError, match="fake ollama failure"):
        fake_ollama.chat(model="argus-qwen25-14b-toolplan:latest", messages=[], fail=True)

    shutdown()
    assert fake_ollama.chat is original

    events = _read_events(path)
    assert [event["event_type"] for event in events] == ["llm.request", "llm.error"]
    assert events[0]["source"]["sdk"] == "ollama"
    assert events[0]["data"]["llm"]["operation"] == "chat"
    assert events[0]["data"]["llm"]["model"] == "argus-qwen25-14b-toolplan:latest"
    assert events[0]["data"]["llm"]["metadata"]["client"] == "module"
    assert "input_hash" in events[0]["data"]["llm"]["input"]
    assert "response_hash" in events[0]["data"]["llm"]["output"]
    assert events[1]["status"] == "error"
    assert events[1]["error"]["type"] == "RuntimeError"


def test_ollama_client_chat_emits_request(tmp_path, monkeypatch):
    _, Client = _install_fake_ollama(monkeypatch)
    original = Client.chat
    path = tmp_path / "events.jsonl"

    result = register(
        project="test-ollama-client",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        instrument_openai_agents=False,
        capture_prompt=False,
        capture_response=False,
    )
    assert result["instrumentors"]["ollama"] is True
    assert Client.chat is not original

    client = Client()
    assert client.chat(model="argus-qwen25-14b-toolplan:latest", messages=[]).model_dump()["message"]["content"] == "client ok"

    shutdown()
    assert Client.chat is original

    events = _read_events(path)
    assert [event["event_type"] for event in events] == ["llm.request"]
    assert events[0]["source"]["sdk"] == "ollama"
    assert events[0]["data"]["llm"]["operation"] == "Client.chat"
    assert events[0]["data"]["llm"]["metadata"]["client"] == "client"
