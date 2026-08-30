import json
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.sdk import MockLLMClient


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_llm_request_event_includes_messages_hash(tmp_path):
    path = tmp_path / "events.jsonl"
    result = register(
        project="test-argus-sdk-llm",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=True,
        capture_arguments=True,
        capture_result=False,
    )
    assert result["instrumentors"]["argus_sdk"] is True

    client = MockLLMClient()
    request = {"tool_results": {}}
    client.generate_answer(request)

    shutdown()

    events = _read_events(path)
    llm_events = [e for e in events if e["event_type"] == "llm.request"]
    assert len(llm_events) == 1
    messages_hash = llm_events[0]["data"]["llm"]["messages_hash"]
    assert messages_hash == sha256_value(request)


def test_ollama_style_response_extracts_prompt_and_eval_counts(tmp_path):
    from senda_argus_hooks.sdk import OllamaClient

    path = tmp_path / "events.jsonl"
    register(
        project="test-argus-sdk-ollama-usage",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=True,
        capture_arguments=True,
        capture_result=False,
    )

    client = OllamaClient()
    import urllib.request

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {
                    "message": {"content": "hi", "tool_calls": []},
                    "prompt_eval_count": 42,
                    "eval_count": 17,
                }
            ).encode("utf-8")

    _orig_urlopen = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _Resp()
    try:
        client.chat([{"role": "user", "content": "hi"}])
    finally:
        urllib.request.urlopen = _orig_urlopen

    shutdown()

    events = _read_events(path)
    llm_events = [e for e in events if e["event_type"] == "llm.request"]
    assert len(llm_events) == 1
    assert llm_events[0]["data"]["llm"]["usage"] == {"input_tokens": 42, "output_tokens": 17}


def test_different_requests_produce_different_messages_hash(tmp_path):
    path = tmp_path / "events.jsonl"
    register(
        project="test-argus-sdk-llm-2",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_mcp=False,
        instrument_argus_sdk=True,
        capture_arguments=True,
        capture_result=False,
    )

    client = MockLLMClient()
    client.generate_answer({"tool_results": {}})
    client.generate_answer({"tool_results": {"vulnerability_intelligence": {"summary": "x"}}})

    shutdown()

    events = _read_events(path)
    llm_events = [e for e in events if e["event_type"] == "llm.request"]
    assert len(llm_events) == 2
    hashes = {e["data"]["llm"]["messages_hash"] for e in llm_events}
    assert len(hashes) == 2
