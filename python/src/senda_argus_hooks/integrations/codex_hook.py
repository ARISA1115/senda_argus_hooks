"""Codex の PreToolUse と PostToolUse の hook のアダプタ。

入力と応答の形は Claude Code と同じで、PostToolUse は tool_response を持つ。許可のときは何も書かない。
"""

from __future__ import annotations

from typing import Any

from .claude_code_hook import parse_claude_style, render_pre_tool_use_deny
from .coding_agent_hooks import ProductAdapter, ToolCall


class CodexAdapter(ProductAdapter):
    product = "codex"

    def parse_pre(self, data: dict[str, Any]) -> ToolCall:
        return parse_claude_style(self.product, data, with_response=False)

    def parse_post(self, data: dict[str, Any]) -> ToolCall:
        return parse_claude_style(self.product, data, with_response=True)

    def render_deny(self, reason: str) -> dict[str, Any]:
        return render_pre_tool_use_deny(reason)
