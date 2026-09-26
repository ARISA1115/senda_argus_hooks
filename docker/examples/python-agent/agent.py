"""Minimal smoke app. Replace this file with the customer's Agent application."""
from senda_argus_hooks.core.runtime import emit_event

emit_event(
    "agent.run.started",
    source={"component": "example", "operation": "startup"},
    data={"agent": {"name": "docker-smoke-agent"}},
    status="started",
)
print("example Python Agent started")
