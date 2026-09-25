"""各 producer が response_model を llm.request イベントに含めることの検証。

Argus 側の ModelSwapRule は data.llm.model (リクエスト時に指定したモデル) と
data.llm.response_model (レスポンスが自己申告した実モデル) の不一致でモデル
すり替えを検知する。producer 側が response_model を送らないとこのルールは
永久に発火しないため、送出経路をここで固定する。
"""

import json
import sys
import types
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.integrations.langchain import SendaArgusCallbackHandler


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.model = payload.get("model")

    def model_dump(self):
        return self.payload


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_openai_instrumentor_emits_response_model_on_swap(tmp_path, monkeypatch):
    class Completions:
        def create(self, *args, **kwargs):
            # リクエストで指定されたモデルと異なるモデルが応答するすり替えを再現する
            return _Response({"id": "chatcmpl_fake", "model": "gpt-3.5-turbo"})

    fake_openai = types.ModuleType("openai")
    fake_openai.resources = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(Completions=Completions)),
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    path = tmp_path / "events.jsonl"
    register(
        project="test-openai-swap",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )
    Completions().create(model="gpt-4", messages=[])
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["model"] == "gpt-4"
    assert llm_event["data"]["llm"]["response_model"] == "gpt-3.5-turbo"


def test_anthropic_instrumentor_emits_response_model_on_swap(tmp_path, monkeypatch):
    class Messages:
        def create(self, *args, **kwargs):
            return _Response({"id": "msg_fake", "model": "claude-2"})

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.resources = types.SimpleNamespace(
        messages=types.SimpleNamespace(Messages=Messages),
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    path = tmp_path / "events.jsonl"
    register(
        project="test-anthropic-swap",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )
    Messages().create(model="claude-3-opus", messages=[])
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["model"] == "claude-3-opus"
    assert llm_event["data"]["llm"]["response_model"] == "claude-2"


def test_litellm_instrumentor_emits_response_model_on_swap(tmp_path, monkeypatch):
    def completion(*args, **kwargs):
        return {"id": "litellm_fake", "model": "gpt-3.5-turbo"}

    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

    path = tmp_path / "events.jsonl"
    register(
        project="test-litellm-swap",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        capture_prompt=False,
        capture_response=False,
    )
    fake_litellm.completion(model="gpt-4", messages=[])
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["model"] == "gpt-4"
    assert llm_event["data"]["llm"]["response_model"] == "gpt-3.5-turbo"


def test_langchain_handler_emits_requested_and_response_model(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test-langchain-swap", exporters=[{"type": "jsonl", "path": str(path)}])
    handler = SendaArgusCallbackHandler()

    handler.on_chat_model_start(
        {"name": "fake_chat"},
        [["hello"]],
        run_id="swap-1",
        invocation_params={"model": "gpt-4"},
    )
    handler.on_llm_end(
        {"generations": ["hi"], "llm_output": {"model_name": "gpt-3.5-turbo"}},
        run_id="swap-1",
    )
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["model"] == "gpt-4"
    assert llm_event["data"]["llm"]["response_model"] == "gpt-3.5-turbo"


def test_langchain_handler_reads_model_from_serialized_kwargs(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test-langchain-serialized", exporters=[{"type": "jsonl", "path": str(path)}])
    handler = SendaArgusCallbackHandler()

    handler.on_llm_start(
        {"name": "fake_llm", "kwargs": {"model_name": "claude-3-opus"}},
        ["hello"],
        run_id="swap-2",
    )
    handler.on_llm_end(
        {"generations": ["hi"], "llm_output": {"model": "claude-3-opus"}},
        run_id="swap-2",
    )
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["model"] == "claude-3-opus"
    assert llm_event["data"]["llm"]["response_model"] == "claude-3-opus"


def test_langchain_handler_omits_fields_when_unavailable(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test-langchain-nomodel", exporters=[{"type": "jsonl", "path": str(path)}])
    handler = SendaArgusCallbackHandler()

    handler.on_llm_start({"name": "fake_llm"}, ["hello"], run_id="swap-3")
    handler.on_llm_end({"generations": ["hi"]}, run_id="swap-3")
    shutdown()

    events = _read_events(path)
    llm_event = next(e for e in events if e["event_type"] == "llm.request")
    assert "model" not in llm_event["data"]["llm"]
    assert "response_model" not in llm_event["data"]["llm"]
