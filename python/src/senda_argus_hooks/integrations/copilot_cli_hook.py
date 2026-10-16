"""GitHub Copilot CLI の preToolUse と postToolUse の hook のアダプタ。

入力は 2 つの形がある。camelCase の名前の設定では sessionId、cwd、toolName、toolArgs、toolResult で、
toolArgs は JSON の文字列のことがある。PascalCase の名前の設定では session_id、tool_name、tool_input、
tool_result になる。呼び出しの識別子はどちらにも無い。失敗の結果は error か tool_result の外に届く。
許可のときは何も書かず、製品の通常の許可の流れへ進める。
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


def _tool_args(data: dict[str, Any]) -> Any:
    if "toolArgs" in data:
        return data.get("toolArgs")
    return data.get("tool_input")


class CopilotCliAdapter(ProductAdapter):
    product = "copilot_cli"

    def _parse(self, data: dict[str, Any]) -> ToolCall:
        return ToolCall(
            product=self.product,
            session_id=required_str(data, "sessionId", "session_id"),
            tool_name=required_str(data, "toolName", "tool_name"),
            tool_input=coerce_tool_input(_tool_args(data)),
            cwd=optional_str(data.get("cwd")),
        )

    def parse_pre(self, data: dict[str, Any]) -> ToolCall:
        return self._parse(data)

    def parse_post(self, data: dict[str, Any]) -> ToolCall:
        call = self._parse(data)
        for key in ("toolResult", "tool_result"):
            if key in data:
                call.tool_response = data.get(key)
                call.has_response = True
                return call
        if "error" in data:
            call.tool_response = {"error": data.get("error")}
            call.has_response = True
        # 結果が無くても実行の後の記録は送る。記録が届かないと静かな欠落に見えるため。
        return call

    def render_deny(self, reason: str) -> dict[str, Any]:
        return {"permissionDecision": "deny", "permissionDecisionReason": reason}
