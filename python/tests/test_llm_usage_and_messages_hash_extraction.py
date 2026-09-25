import json
import sys
import types
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core.hashing import sha256_value


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.usage = payload.get("usage")

    def model_dump(self):
        return self.payload


def test_openai_llm_request_includes_usage(tmp_path, monkeypatch):
    class Completions:
        def create(self, *args, **kwargs):
            return _Response({"id": "chatcmpl_fake", "model": kwargs.get("model"), "usage": {"prompt_tokens": 12, "completion_tokens": 34}})

    fake_openai = types.ModuleType("openai")
    fake_openai.resources = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(Completions=Completions)),
        responses=types.SimpleNamespace(Responses=type("Responses", (), {"create": lambda self, *a, **k: _Response({})})),
        embeddings=types.SimpleNamespace(Embeddings=type("Embeddings", (), {"create": lambda self, *a, **k: _Response({})})),
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    path = tmp_path / "events.jsonl"
    register(
        project="test-openai-usage",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )

    client = Completions()
    client.create(model="gpt-fake", messages=[{"role": "user", "content": "hi"}])

    shutdown()

    events = _read_events(path)
    llm = events[0]["data"]["llm"]
    assert llm["usage"] == {"input_tokens": 12, "output_tokens": 34}
    assert llm["messages_hash"] == sha256_value([{"role": "user", "content": "hi"}])


def test_anthropic_llm_request_includes_usage(tmp_path, monkeypatch):
    class Messages:
        def create(self, *args, **kwargs):
            return _Response({"id": "msg_fake", "model": kwargs.get("model"), "usage": {"input_tokens": 5, "output_tokens": 7}})

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.resources = types.SimpleNamespace(messages=types.SimpleNamespace(Messages=Messages))
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    path = tmp_path / "events.jsonl"
    register(
        project="test-anthropic-usage",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )

    client = Messages()
    client.create(model="claude-fake", messages=[{"role": "user", "content": "hi"}])

    shutdown()

    events = _read_events(path)
    llm = events[0]["data"]["llm"]
    assert llm["usage"] == {"input_tokens": 5, "output_tokens": 7}


def test_litellm_llm_request_includes_usage_and_messages_hash(tmp_path, monkeypatch):
    fake_litellm = types.ModuleType("litellm")

    def completion(*args, **kwargs):
        return {"id": "litellm_fake", "model": kwargs.get("model"), "usage": {"prompt_tokens": 8, "completion_tokens": 16}}

    def embedding(*args, **kwargs):
        return {"id": "embedding_fake", "model": kwargs.get("model")}

    fake_litellm.completion = completion
    fake_litellm.embedding = embedding
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

    path = tmp_path / "events.jsonl"
    register(
        project="test-litellm-usage",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )

    import litellm
    messages = [{"role": "user", "content": "hi"}]
    litellm.completion(model="gpt-fake", messages=messages)

    shutdown()

    events = _read_events(path)
    llm = events[0]["data"]["llm"]
    assert llm["usage"] == {"input_tokens": 8, "output_tokens": 16}
    assert llm["messages_hash"] == sha256_value(messages)
