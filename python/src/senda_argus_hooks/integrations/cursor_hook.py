"""Cursor の preToolUse と postToolUse と postToolUseFailure の hook のアダプタ。

共通の入力は conversation_id と user_email を持ち、preToolUse は tool_name、tool_input、tool_use_id、cwd を
持つ。postToolUse は tool_output、postToolUseFailure は error_message と failure_type を持つ。
Cursor の許可の hook は、出力が無いか形の合わない出力を遮断として扱うため、許可のときも
permission allow を書く。
"""

from __future__ import annotations

from typing import Any

from .coding_agent_hooks import (
    ProductAdapter,
    ToolCall,
    coerce_tool_input,
    optional_str,
    required_str,
)


class CursorAdapter(ProductAdapter):
    product = "cursor"

    def _parse(self, data: dict[str, Any]) -> ToolCall:
        return ToolCall(
            product=self.product,
            session_id=required_str(data, "conversation_id", "session_id"),
            tool_name=required_str(data, "tool_name"),
            tool_input=coerce_tool_input(data.get("tool_input")),
            tool_use_id=optional_str(data.get("tool_use_id")),
            cwd=optional_str(data.get("cwd")),
            user_email=optional_str(data.get("user_email")),
        )

    def parse_pre(self, data: dict[str, Any]) -> ToolCall:
        return self._parse(data)

    def parse_post(self, data: dict[str, Any]) -> ToolCall:
        call = self._parse(data)
        if "tool_output" in data:
            call.tool_response = data.get("tool_output")
        else:
            call.tool_response = {
                "error": data.get("error_message"),
                "failure_type": data.get("failure_type"),
            }
        call.has_response = True
        return call

    def render_deny(self, reason: str) -> dict[str, Any]:
        return {"permission": "deny", "user_message": reason, "agent_message": reason}

    def render_allow(self) -> dict[str, Any] | None:
        return {"permission": "allow"}
