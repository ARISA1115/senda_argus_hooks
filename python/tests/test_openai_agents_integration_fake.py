from pathlib import Path
import sys
import types

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.integrations.openai_agents import SendaArgusOpenAIAgentsProcessor


def _events(path: Path):
    import json

    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_openai_agents_processor_emits_trace_and_span_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}])
    processor = SendaArgusOpenAIAgentsProcessor()

    processor.on_trace_start({"name": "run"})
    processor.on_span_start(types.SimpleNamespace(type="tool", name="lookup"))
    processor.on_span_end(types.SimpleNamespace(type="tool", name="lookup"))
    processor.on_trace_end({"name": "run"})
    shutdown()

    event_types = [event["event_type"] for event in _events(path)]
    assert event_types == ["agent.run.started", "tool_call.requested", "tool_call.completed", "agent.run.completed"]


def test_openai_agents_runner_instrumentor_sync_success_and_failure(tmp_path: Path, monkeypatch):
    path = tmp_path / "events.jsonl"

    class Runner:
        @staticmethod
        def run_sync(payload=None, fail=False):
            if fail:
                raise RuntimeError("runner failed")
            return {"ok": True, "payload": payload}

    fake_agents = types.ModuleType("agents")
    fake_agents.Runner = Runner
    monkeypatch.setitem(sys.modules, "agents", fake_agents)

    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], auto_instrument=True, capture_result=True)
    assert Runner.run_sync({"task": "hello"})["ok"] is True
    try:
        Runner.run_sync({"task": "hello"}, fail=True)
    except RuntimeError:
        pass
    shutdown()

    event_types = [event["event_type"] for event in _events(path)]
    assert event_types == [
        "agent.run.started",
        "agent.run.completed",
        "agent.run.started",
        "agent.run.failed",
    ]
