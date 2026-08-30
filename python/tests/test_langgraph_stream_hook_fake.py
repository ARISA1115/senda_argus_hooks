from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.integrations.langgraph import stream_with_argus


def _events(path: Path):
    import json

    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class FakeGraph:
    name = "fake_graph"

    def stream(self, input_data, *args, **kwargs):
        yield {"node": "triage", "input": input_data}
        yield {"node": "answer", "status": "ok"}


class FailedGraph:
    name = "failed_graph"

    def stream(self, input_data, *args, **kwargs):
        yield {"node": "start"}
        raise RuntimeError("graph failed")


def test_langgraph_stream_with_argus_emits_run_and_step_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], capture_result=True)

    chunks = list(stream_with_argus(FakeGraph(), {"question": "hello"}, stream_mode="updates"))
    shutdown()

    assert len(chunks) == 2
    event_types = [event["event_type"] for event in _events(path)]
    assert event_types == [
        "agent.run.started",
        "agent.step.completed",
        "agent.step.completed",
        "agent.run.completed",
    ]


def test_langgraph_stream_with_argus_emits_failed_event(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}])

    try:
        list(stream_with_argus(FailedGraph(), {"question": "hello"}))
    except RuntimeError:
        pass
    shutdown()

    event_types = [event["event_type"] for event in _events(path)]
    assert event_types == ["agent.run.started", "agent.step.completed", "agent.run.failed"]
