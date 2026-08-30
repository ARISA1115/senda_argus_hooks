from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    schema_version: str
    event_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    timestamp: str
    project: str
    environment: str
    event_type: str
    tenant_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    purpose_id: str | None = None
    source: dict[str, Any] = field(default_factory=dict)
    actor: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    latency_ms: int | None = None
    error: dict[str, Any] | None = None
    runtime: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_event(
    *,
    project: str,
    environment: str,
    event_type: str,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    source: dict[str, Any] | None = None,
    actor: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    security: dict[str, Any] | None = None,
    status: str | None = None,
    latency_ms: int | None = None,
    error: dict[str, Any] | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
    agent_id: str | None = None,
    purpose_id: str | None = None,
    runtime: dict[str, Any] | None = None,
) -> Event:
    return Event(
        schema_version="0.2",
        event_id=f"evt_{uuid4().hex}",
        trace_id=trace_id or f"trace_{uuid4().hex}",
        span_id=f"span_{uuid4().hex}",
        parent_span_id=parent_span_id,
        timestamp=utc_now_iso(),
        project=project,
        environment=environment,
        event_type=event_type,
        tenant_id=tenant_id,
        session_id=session_id,
        conversation_id=conversation_id,
        run_id=run_id,
        turn_id=turn_id,
        agent_id=agent_id,
        purpose_id=purpose_id,
        source=source or {},
        actor=actor or {},
        data=data or {},
        security=security or {},
        status=status,
        latency_ms=latency_ms,
        error=error,
        runtime=runtime or {},
    )
