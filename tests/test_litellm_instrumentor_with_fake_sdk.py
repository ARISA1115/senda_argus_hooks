import json
import sys
import types
from pathlib import Path

import pytest

from senda_argus_hooks import register, shutdown


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _install_fake_litellm(monkeypatch):
    fake_litellm = types.ModuleType("litellm")

    def completion(*args, **kwargs):
        if kwargs.get("fail"):
            raise RuntimeError("fake litellm failure")
        return {"id": "litellm_fake", "model": kwargs.get("model")}

    def embedding(*args, **kwargs):
        return {"id": "embedding_fake", "model": kwargs.get("model")}

    fake_litellm.completion = completion
    fake_litellm.embedding = embedding
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    return fake_litellm, completion


def test_litellm_instrumentor_emits_request_error_and_unpatches(tmp_path, monkeypatch):
    fake_litellm, original = _install_fake_litellm(monkeypatch)
    path = tmp_path / "events.jsonl"

    result = register(
        project="test-litellm",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )
    assert result["instrumentors"]["litellm"] is True
    assert fake_litellm.completion is not original

    assert fake_litellm.completion(model="gpt-fake", messages=[])["model"] == "gpt-fake"
    with pytest.raises(RuntimeError, match="fake litellm failure"):
        fake_litellm.completion(model="gpt-fake", messages=[], fail=True)

    shutdown()
    assert fake_litellm.completion is original

    events = _read_events(path)
    assert [event["event_type"] for event in events] == ["llm.request", "llm.error"]
    assert events[0]["source"]["sdk"] == "litellm"
    assert events[0]["data"]["llm"]["operation"] == "completion"
    assert events[0]["data"]["llm"]["model"] == "gpt-fake"
    assert events[1]["status"] == "error"
    assert events[1]["error"]["type"] == "RuntimeError"
