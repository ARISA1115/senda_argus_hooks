"""Claude Code の PreToolUse と PostToolUse の hook のアダプタ。

入力は session_id、cwd、tool_name、tool_input、tool_use_id を持つ。PostToolUse は tool_response を持つ。
許可のときは何も書かない。permissionDecision allow を返すと Claude Code の利用者の許可の確認を
飛ばすため、Argus の許可は通常の許可の流れへ進めるだけにする。
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


def parse_claude_style(
    product: str, data: dict[str, Any], *, with_response: bool
) -> ToolCall:
    """Claude Code と同じ形の入力を読む。Codex も同じ形を使う。"""
    call = ToolCall(
        product=product,
        session_id=required_str(data, "session_id"),
        tool_name=required_str(data, "tool_name"),
        tool_input=coerce_tool_input(data.get("tool_input")),
        tool_use_id=optional_str(data.get("tool_use_id")),
        cwd=optional_str(data.get("cwd")),
    )
    if with_response:
        call.tool_response = data.get("tool_response")
        call.has_response = "tool_response" in data
    return call


def render_pre_tool_use_deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class ClaudeCodeAdapter(ProductAdapter):
    product = "claude_code"

    def parse_pre(self, data: dict[str, Any]) -> ToolCall:
        return parse_claude_style(self.product, data, with_response=False)

    def parse_post(self, data: dict[str, Any]) -> ToolCall:
        return parse_claude_style(self.product, data, with_response=True)

    def render_deny(self, reason: str) -> dict[str, Any]:
        return render_pre_tool_use_deny(reason)
