from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class RuntimeConfig:
    project: str = "default"
    environment: str = "dev"
    capture_prompt: bool = True
    capture_response: bool = True
    capture_arguments: bool = True
    capture_result: bool = True
    capture_hash: bool = True
    redact: bool = True
    actor: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    purpose_id: str | None = None
    agent_hint: str | None = None


def new_run_id() -> str:
    return f"run_{uuid4().hex}"


_current_trace_id: ContextVar[str | None] = ContextVar("senda_trace_id", default=None)
_current_span_id: ContextVar[str | None] = ContextVar("senda_span_id", default=None)
_current_run_id: ContextVar[str | None] = ContextVar("senda_run_id", default=None)
_current_turn_id: ContextVar[str | None] = ContextVar("senda_turn_id", default=None)
_current_agent_id: ContextVar[str | None] = ContextVar("senda_agent_id", default=None)
_current_purpose_id: ContextVar[str | None] = ContextVar("senda_purpose_id", default=None)


def get_trace_id() -> str | None:
    return _current_trace_id.get()


def set_trace_id(trace_id: str | None):
    return _current_trace_id.set(trace_id)


def reset_trace_id(token) -> None:
    _current_trace_id.reset(token)


def get_parent_span_id() -> str | None:
    return _current_span_id.get()


def set_span_id(span_id: str | None):
    return _current_span_id.set(span_id)


def reset_span_id(token) -> None:
    _current_span_id.reset(token)


def get_run_id() -> str | None:
    return _current_run_id.get()


def set_run_id(run_id: str | None):
    return _current_run_id.set(run_id)


def reset_run_id(token) -> None:
    _current_run_id.reset(token)


def get_turn_id() -> str | None:
    return _current_turn_id.get()


def set_turn_id(turn_id: str | None):
    return _current_turn_id.set(turn_id)


def reset_turn_id(token) -> None:
    _current_turn_id.reset(token)


def get_agent_id() -> str | None:
    return _current_agent_id.get()


def set_agent_id(agent_id: str | None):
    return _current_agent_id.set(agent_id)


def reset_agent_id(token) -> None:
    _current_agent_id.reset(token)


def get_purpose_id() -> str | None:
    return _current_purpose_id.get()


def set_purpose_id(purpose_id: str | None):
    return _current_purpose_id.set(purpose_id)


def reset_purpose_id(token) -> None:
    _current_purpose_id.reset(token)
