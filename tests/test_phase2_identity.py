import json
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.sdk import MockMCPClient
from senda_argus_hooks.core.identity import derive_purpose_id


def test_phase2_schema_identity_fields(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="phase2", environment="dev", exporters=[{"type": "jsonl", "path": str(path)}], auto_instrument=True)
    client = MockMCPClient({"lookup": lambda query: {"ok": True, "query": query}}, server="nexus-mcp")
    client.call_tool("lookup", {"query": "CVE-2024-3094"}, capability="vulnerability_intelligence")
    shutdown()

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["event_type"] for e in events] == ["mcp.tool_call.requested", "mcp.tool_call.completed"]
    completed = events[-1]
    assert completed["schema_version"] == "0.2"
    assert completed["agent_id"].startswith("agent_")
    assert completed["purpose_id"].startswith("purpose_")
    assert completed["data"]["mcp"]["mcp_profile_id"].startswith("mcp_profile_")


def test_purpose_id_groups_same_mcp_capability():
    one = derive_purpose_id(mcp_server_name="nexus-mcp", mcp_server_url="https://example.com/mcp/", tool_name="lookup", capability="triage")
    two = derive_purpose_id(mcp_server_name="nexus-mcp", mcp_server_url="https://EXAMPLE.com/mcp", tool_name="lookup", capability="triage")
    assert one == two
