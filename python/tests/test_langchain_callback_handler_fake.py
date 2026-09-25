from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.integrations.langchain import SendaArgusCallbackHandler


def _events(path: Path):
    import json

    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_langchain_callback_handler_emits_llm_tool_and_agent_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], capture_arguments=True, capture_result=True)
    handler = SendaArgusCallbackHandler()

    handler.on_llm_start({"name": "fake_llm"}, ["hello"], run_id="llm-1")
    handler.on_llm_end({"generations": ["hi"]}, run_id="llm-1")
    handler.on_tool_start({"name": "lookup_cve", "tool_type": "http_api"}, {"cve": "CVE-2024-3094"}, run_id="tool-1")
    handler.on_tool_end({"ok": True}, run_id="tool-1", name="lookup_cve")
    handler.on_agent_action({"tool": "lookup_cve"})
    handler.on_agent_finish({"output": "done"})
    shutdown()

    event_types = [event["event_type"] for event in _events(path)]
    assert event_types == [
        "llm.request.started",
        "llm.request",
        "tool_call.requested",
        "tool_call.completed",
        "agent.decision",
        "agent.run.completed",
    ]
    tool_events = [event for event in _events(path) if event["event_type"].startswith("tool_call")]
    assert tool_events[0]["data"]["tool"]["purpose_id"].startswith("purpose_")
    # on_tool_start と on_tool_end が同一 run_id の tool_type を共有するため、
    # requested と completed の purpose_id が一致する (以前は completed 側が
    # tool_type を "unknown" に固定していたため一致しなかった)。
    assert tool_events[0]["data"]["tool"]["purpose_id"] == tool_events[1]["data"]["tool"]["purpose_id"]

    decision_event = next(event for event in _events(path) if event["event_type"] == "agent.decision")
    assert "selected_tool_purpose_id" in decision_event["data"]


def test_on_llm_end_extracts_token_usage(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], capture_arguments=True, capture_result=True)
    handler = SendaArgusCallbackHandler()

    handler.on_llm_start({"name": "fake_llm"}, ["hello"], run_id="llm-usage-1")
    handler.on_llm_end(
        {"generations": ["hi"], "llm_output": {"token_usage": {"prompt_tokens": 11, "completion_tokens": 22}}},
        run_id="llm-usage-1",
    )
    shutdown()

    llm_event = next(event for event in _events(path) if event["event_type"] == "llm.request")
    assert llm_event["data"]["llm"]["usage"] == {"input_tokens": 11, "output_tokens": 22}


def test_agent_decision_purpose_id_matches_tool_call_when_no_explicit_type(tmp_path: Path):
    """AgentAction には tool_type が無いため、on_agent_action は _tool_type() の既定値
    "function" に揃える。tool_type を明示しない tool 呼び出しと purpose_id が一致することを固定する。"""
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}])
    handler = SendaArgusCallbackHandler()

    handler.on_agent_action({"tool": "search_web"})
    handler.on_tool_start({"name": "search_web"}, {"query": "senda-argus"}, run_id="tool-2")
    handler.on_tool_end({"ok": True}, run_id="tool-2", name="search_web")
    shutdown()

    events = _events(path)
    decision_event = next(e for e in events if e["event_type"] == "agent.decision")
    requested_event = next(e for e in events if e["event_type"] == "tool_call.requested")
    assert decision_event["data"]["selected_tool_purpose_id"] == requested_event["data"]["tool"]["purpose_id"]


def test_langchain_callback_handler_emits_error_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}])
    handler = SendaArgusCallbackHandler()

    handler.on_llm_start({"name": "fake_llm"}, ["hello"], run_id="llm-err")
    handler.on_llm_error(RuntimeError("llm failed"), run_id="llm-err")
    handler.on_tool_start({"name": "run_command"}, "whoami", run_id="tool-err")
    handler.on_tool_error(ValueError("tool failed"), run_id="tool-err", name="run_command")
    shutdown()

    event_types = [event["event_type"] for event in _events(path)]
    assert "llm.error" in event_types
    assert "tool_call.failed" in event_types
