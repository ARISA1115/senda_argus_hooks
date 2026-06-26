from __future__ import annotations

from typing import Any


class PromptOpsClient:
    """SDK-style PromptOps lifecycle client.

    Methods only return their payloads. Argus events are emitted by the SDK
    instrumentor that wraps these methods.
    """

    def agent_decision(self, *, task_id: str | None = None, agent_id: str | None = None, selected_tool: str | None = None, reason: str | None = None, alternatives: list[str] | None = None, risk_level: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "task_id": task_id,
            "agent_id": agent_id,
            "selected_tool": selected_tool,
            "reason": reason,
            "alternatives": alternatives or [],
            "risk_level": risk_level,
        }
        if extra:
            payload.update(extra)
        return payload

    def run_completed(self, **payload: Any) -> dict[str, Any]:
        return dict(payload)
