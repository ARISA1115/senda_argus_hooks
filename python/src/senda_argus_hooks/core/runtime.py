from __future__ import annotations

from typing import Any

from .context import (
    RuntimeConfig,
    get_agent_id,
    get_parent_span_id,
    get_purpose_id,
    get_run_id,
    get_trace_id,
    get_turn_id,
    new_run_id,
    reset_run_id,
    reset_span_id,
    reset_trace_id,
    set_run_id,
    set_span_id,
    set_trace_id,
)
from .event import new_event
from .identity import derive_agent_id, runtime_metadata
from .queue import EventBus
from .redaction import redact_event

_config = RuntimeConfig()
_bus = EventBus(exporters=[])
_runtime_metadata = runtime_metadata()


def configure(config: RuntimeConfig, bus: EventBus) -> None:
    global _config, _bus
    _config = config
    _bus = bus


def get_config() -> RuntimeConfig:
    return _config


def get_bus() -> EventBus:
    return _bus


def effective_agent_id(source: dict[str, Any] | None = None, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if get_agent_id():
        return get_agent_id() or ""
    if _config.agent_id:
        return _config.agent_id
    sdk = (source or {}).get("sdk")
    return derive_agent_id(project=_config.project, environment=_config.environment, sdk=sdk, agent_hint=_config.agent_hint)


def emit_event(
    event_type: str,
    data: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    actor: dict[str, Any] | None = None,
    status: str | None = None,
    latency_ms: int | None = None,
    error: dict[str, Any] | None = None,
    purpose_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    event = new_event(
        project=_config.project,
        environment=_config.environment,
        event_type=event_type,
        trace_id=get_trace_id(),
        parent_span_id=get_parent_span_id(),
        source=source or {},
        actor=actor or _config.actor,
        data=data or {},
        security={"redacted": False},
        status=status,
        latency_ms=latency_ms,
        error=error,
        tenant_id=_config.tenant_id,
        session_id=_config.session_id,
        conversation_id=_config.conversation_id,
        run_id=get_run_id() or _config.run_id,
        turn_id=get_turn_id() or _config.turn_id,
        agent_id=effective_agent_id(source, agent_id),
        purpose_id=purpose_id or get_purpose_id(),
        runtime=_runtime_metadata,
    ).to_dict()
    if _config.redact:
        event = redact_event(event)
    _bus.emit(event)
    return event


class span_context:
    def __init__(self, event_type: str, data: dict[str, Any] | None = None, source: dict[str, Any] | None = None):
        self.event_type = event_type
        self.data = data or {}
        self.source = source or {}
        self.trace_token = None
        self.span_token = None
        self.run_token = None
        self.event = None

    def __enter__(self):
        if get_run_id() is None:
            self.run_token = set_run_id(_config.run_id or new_run_id())
        self.event = emit_event(self.event_type + ".start", data=self.data, source=self.source, status="start")
        self.trace_token = set_trace_id(self.event["trace_id"])
        self.span_token = set_span_id(self.event["span_id"])
        return self

    def __exit__(self, exc_type, exc, tb):
        err = None
        status = "success"
        if exc is not None:
            status = "error"
            err = {"type": exc.__class__.__name__, "message": str(exc)}
        emit_event(self.event_type + ".end", data=self.data, source=self.source, status=status, error=err)
        if self.span_token is not None:
            reset_span_id(self.span_token)
        if self.trace_token is not None:
            reset_trace_id(self.trace_token)
        if self.run_token is not None:
            reset_run_id(self.run_token)
        return False
