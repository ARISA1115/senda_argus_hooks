from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.runtime import emit_event, span_context


def event(event_type: str, data: dict[str, Any] | None = None, source: dict[str, Any] | None = None, status: str | None = "success") -> dict[str, Any]:
    return emit_event(event_type, data=data or {}, source=source or {"component": "custom"}, status=status)


def agent_decision(*, task_id: str | None = None, agent_id: str | None = None, selected_tool: str | None = None, selected_tool_purpose_id: str | None = None, reason: str | None = None, alternatives: list[str | dict[str, Any]] | None = None, risk_level: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """agent.decision を送信する。

    selected_tool_purpose_id は mcp_tool_call() / _wrap_mcp が derive_purpose_id() で
    計算するのと同じ purpose_id を渡すと、IntentExecutionMismatchRule が
    mcp.tool_call.requested の data.mcp.purpose_id と突合できる。呼び出し元が
    選択した tool の server/capability を把握している場合に
    core.identity.derive_purpose_id() で計算して渡す。
    """
    data = {
        "task_id": task_id,
        "agent_id": agent_id,
        "selected_tool": selected_tool,
        "selected_tool_purpose_id": selected_tool_purpose_id,
        "reason": reason,
        "alternatives": alternatives or [],
        "risk_level": risk_level,
    }
    if extra:
        data.update(extra)
    return emit_event("agent.decision", data=data, source={"component": "custom"}, status="success")


@contextmanager
def span(event_type: str, data: dict[str, Any] | None = None, source: dict[str, Any] | None = None):
    with span_context(event_type, data=data, source=source) as ctx:
        yield ctx


@contextmanager
def mcp_tool_call(*, server: str, tool: str, arguments: dict[str, Any] | None = None, capability: str | None = None):
    args = arguments or {}
    with span_context(
        "mcp.tool_call",
        data={
            "mcp": {
                "server": server,
                "tool": tool,
                "capability": capability,
                "arguments": args,
                "arguments_hash": sha256_value(args),
            }
        },
        source={"component": "custom_mcp"},
    ):
        yield
