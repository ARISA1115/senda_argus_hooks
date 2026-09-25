from senda_argus_hooks import audit, register, shutdown

register(
    project="example-agent",
    exporters=[
        {"type": "jsonl", "path": "./logs/events.jsonl"},
        {"type": "parquet", "dir": "./logs/parquet", "flush_size": 2},
    ],
)

audit.event("custom.event", {"message": "hello"})
audit.agent_decision(
    task_id="task-001",
    agent_id="sec-agent-001",
    selected_tool="lookup_cve",
    reason="CVE情報を取得するため",
)

with audit.mcp_tool_call(server="nvd", tool="lookup_cve", arguments={"cve": "CVE-2024-3094"}):
    pass

shutdown()
