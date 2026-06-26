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


def _install_fake_anthropic(monkeypatch):
    class Messages:
        def create(self, *args, **kwargs):
            if kwargs.get("fail"):
                raise RuntimeError("fake anthropic failure")
            return _Response({"id": "msg_fake", "model": kwargs.get("model")})

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.resources = types.SimpleNamespace(messages=types.SimpleNamespace(Messages=Messages))
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    return fake_anthropic, Messages


def test_anthropic_instrumentor_emits_request_error_and_unpatches(tmp_path, monkeypatch):
    _, Messages = _install_fake_anthropic(monkeypatch)
    original = Messages.create
    path = tmp_path / "events.jsonl"

    result = register(
        project="test-anthropic",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )
    assert result["instrumentors"]["anthropic"] is True
    assert Messages.create is not original

    client = Messages()
    assert client.create(model="claude-fake", messages=[]).model_dump()["model"] == "claude-fake"
    with pytest.raises(RuntimeError, match="fake anthropic failure"):
        client.create(model="claude-fake", messages=[], fail=True)

    shutdown()
    assert Messages.create is original

    events = _read_events(path)
    assert [event["event_type"] for event in events] == ["llm.request", "llm.error"]
    assert events[0]["source"]["sdk"] == "anthropic"
    assert events[0]["data"]["llm"]["operation"] == "messages.create"
    assert events[0]["data"]["llm"]["model"] == "claude-fake"
    assert events[1]["status"] == "error"
    assert events[1]["error"]["type"] == "RuntimeError"
