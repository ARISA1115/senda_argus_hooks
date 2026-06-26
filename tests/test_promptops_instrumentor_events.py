import json
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.sdk import MockMCPClient, PromptOpsClient


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_promptops_and_builtin_mcp_instrumentor_events(tmp_path):
    path = tmp_path / "events.jsonl"
    result = register(
        project="test-promptops",
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

    promptops = PromptOpsClient()
    mcp = MockMCPClient({"lookup": lambda query: {"ok": True, "query": query}}, server="mock_mcp")

    decision = promptops.agent_decision(task_id="task-1", selected_tool="lookup", reason="need context")
    assert decision["selected_tool"] == "lookup"
    assert mcp.call_tool("lookup", {"query": "CVE-2024-3094"}, capability="vulnerability_intelligence")["ok"] is True
    completed = promptops.run_completed(run_id="run-1", status="success")
    assert completed["status"] == "success"

    shutdown()

    events = _read_events(path)
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "agent.decision",
        "mcp.tool_call.requested",
        "mcp.tool_call.completed",
        "promptops.run.completed",
    ]
    assert events[0]["source"]["sdk"] == "senda_argus_hooks.sdk"
    assert events[1]["data"]["mcp"]["tool"] == "lookup"
    assert events[1]["purpose_id"].startswith("purpose_")
    assert events[2]["data"]["mcp"]["result_hash"]
    assert events[3]["data"]["run_id"] == "run-1"
