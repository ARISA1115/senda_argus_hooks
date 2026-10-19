"""製品ごとの hook の入力の読み方と応答の書き方の表。

判定の待ち方、時間の上限、失敗の扱いは coding_agent_hooks に 1 つだけ置き、ここには製品ごとの違い
だけを置く。違いは 2 つの表で持つ。

- 入力の表 InputSpec: 製品の入力のどの項目が、約束の session_id、tool_name、tool_input、tool_use_id、
  cwd、user_email に当たるか。製品が別の名前の項目で運ぶ値 (Antigravity の toolCall、Cline の
  preToolUse) は、ここで tool_name と tool_input へ読み替える
- 応答の形 4 種: hookSpecificOutput の permissionDecision、decision、終了コード 2 だけ、Cline の cancel

各製品の出典は ADAPTERS の行の上に書く。確認は 2026-10-16。公式の文書に書かれていない項目は読まない。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .claude_code_hook import ClaudeCodeAdapter, render_pre_tool_use_deny
from .codex_hook import CodexAdapter
from .coding_agent_hooks import (
    InputError,
    ProductAdapter,
    ToolCall,
    coerce_tool_input,
    optional_str,
    read_permission_mode,
    required_str,
)
from .copilot_cli_hook import CopilotCliAdapter
from .cursor_hook import CursorAdapter


def _dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _container(data: dict[str, Any], key: str | None) -> dict[str, Any]:
    if key is None:
        return data
    inner = data.get(key)
    if not isinstance(inner, dict):
        raise InputError(f"{key} が無い")
    return inner


def process_session_id() -> str:
    """セッションの識別子を入力に持たない製品のための、hook のプロセスの POSIX のセッションの番号。

    製品と、製品が起動した hook は、端末の同じセッションに属する。実行の前と後の hook で同じ値になり、
    別の端末で動く製品とは分かれる。全ての呼び出しを 1 つの当事者に畳まない。
    """
    try:
        return f"process-session:{os.getsid(0)}"
    except OSError as exc:
        raise InputError("セッションの識別子を得られない") from exc


@dataclass(frozen=True)
class InputSpec:
    """製品の入力の項目の名前。tuple は先に見つかった項目を使う。"""

    session: tuple[str, ...] = ("session_id",)
    tool_name: str = "tool_name"
    tool_input: str = "tool_input"
    # tool_name と tool_input を入れた中の項目の名前。実行の前と後で違う製品がある。
    pre_container: str | None = None
    post_container: str | None = None
    tool_use_id: tuple[str, ...] = ()
    cwd: tuple[str, ...] = ()
    user_email: tuple[str, ...] = ()
    # 実行の後の結果の項目。先に見つかったものを tool_response にする。
    response: tuple[str, ...] = ()
    # 失敗を運ぶ項目。結果の項目が無いときに {項目: 値} を tool_response にする。
    error: tuple[str, ...] = ()
    # session の項目が無いときは、hook のプロセスのセッションで区別する。入力にセッションの識別子を
    # 持たない製品と、公式の入力の例にセッションの識別子が無い製品に使う。
    session_from_process: bool = False


class SpecAdapter(ProductAdapter):
    """InputSpec で入力を読み、応答の書き方を下位のクラスで決めるアダプタ。"""

    spec: InputSpec = InputSpec()

    def __init__(self, product: str, spec: InputSpec) -> None:
        self.product = product
        self.spec = spec

    def _parse(self, data: dict[str, Any], *, post: bool) -> ToolCall:
        spec = self.spec
        inner = _container(data, spec.post_container if post else spec.pre_container)
        session_id = _first_str(data, spec.session)
        if session_id is None:
            if not spec.session_from_process:
                raise InputError(f"{(spec.session or ('session_id',))[0]} が無い")
            session_id = process_session_id()
        call = ToolCall(
            product=self.product,
            session_id=session_id,
            tool_name=required_str(inner, spec.tool_name),
            tool_input=coerce_tool_input(inner.get(spec.tool_input)),
            tool_use_id=_first_str(data, spec.tool_use_id),
            cwd=_first_str(data, spec.cwd),
            user_email=optional_str(_dig(data, spec.user_email))
            if spec.user_email
            else None,
            permission_mode=read_permission_mode(self.product, data),
        )
        if post:
            _attach_response(call, inner, data, spec)
        return call

    def parse_pre(self, data: dict[str, Any]) -> ToolCall:
        return self._parse(data, post=False)

    def parse_post(self, data: dict[str, Any]) -> ToolCall:
        return self._parse(data, post=True)


def _first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = optional_str(data.get(key))
        if value:
            return value
    return None


def _attach_response(
    call: ToolCall, inner: dict[str, Any], data: dict[str, Any], spec: InputSpec
) -> None:
    # 結果の項目が null で在るときは、失敗の項目を先に見る。失敗のときに結果の項目が null で届く製品が
    # あっても、失敗の内容を落とさない。
    for source in (inner, data):
        for key in spec.response:
            if source.get(key) is not None:
                call.tool_response = source.get(key)
                call.has_response = True
                return
    for source in (inner, data):
        for key in spec.error:
            if source.get(key) not in (None, ""):
                call.tool_response = {key: source.get(key)}
                call.has_response = True
                return
    if any(key in source for source in (inner, data) for key in spec.response):
        call.has_response = True


class PermissionDecisionAdapter(SpecAdapter):
    """hookSpecificOutput の permissionDecision で遮断を返す製品。許可は何も書かない。

    permissionDecision allow を返すと、製品の利用者の許可の確認を飛ばす製品があるため。
    """

    def render_deny(self, reason: str) -> dict[str, Any]:
        return render_pre_tool_use_deny(reason)


class DecisionAdapter(SpecAdapter):
    """最上位の decision と reason で遮断を返す製品。"""

    def __init__(
        self,
        product: str,
        spec: InputSpec,
        *,
        deny_word: str = "deny",
        allow: dict[str, Any] | None = None,
        deny_exit_code: int = 2,
        post_output: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(product, spec)
        self._deny_word = deny_word
        self._allow = allow
        self._post_output = post_output
        self.deny_exit_code = deny_exit_code

    def render_deny(self, reason: str) -> dict[str, Any]:
        return {"decision": self._deny_word, "reason": reason}

    def render_allow(self) -> dict[str, Any] | None:
        return None if self._allow is None else dict(self._allow)

    def render_post(self) -> dict[str, Any] | None:
        return None if self._post_output is None else dict(self._post_output)


class ExitCodeAdapter(SpecAdapter):
    """終了コード 2 だけが公式の遮断の返し方の製品。標準出力には何も書かず、理由は標準エラー出力へ書く。"""

    def render_deny(self, reason: str) -> None:
        return None


class ClineAdapter(SpecAdapter):
    """Cline の PreToolUse のスクリプト。全ての hook は標準出力に cancel を返すと公式に書かれている。

    終了コードの意味は書かれていない。公式の例は cancel true を書いて終了コード 0 で終わるため、同じにする。
    """

    deny_exit_code = 0

    def render_deny(self, reason: str) -> dict[str, Any]:
        return {"cancel": True, "errorMessage": reason}

    def render_allow(self) -> dict[str, Any]:
        return {"cancel": False}

    def render_post(self) -> dict[str, Any]:
        return {"cancel": False}


# Windsurf の操作ごとの hook の名前から pre_ と post_ を除いた部分。全ツールをまとめる hook は無い。
WINDSURF_ACTIONS = ("run_command", "read_code", "write_code", "mcp_tool_use")
# 実行の後にだけ届く項目。実行の前と同じ引数のハッシュで結ぶため、tool_input から外して結果にする。
_WINDSURF_RESULT_KEYS = ("mcp_result",)


class WindsurfAdapter(ExitCodeAdapter):
    """Windsurf の Cascade の hook。

    入力は agent_action_name、trajectory_id、tool_info で、tool 名を持たない。tool_name には実行の前の
    hook の名前 (pre_run_command など) を、実行の後でも同じ名前で送る。サーバーは実行の前と後を
    tool 名と引数で結ぶため。tool_input は tool_info から実行の後にだけ届く項目を除いた物で、サーバーが
    コマンドと MCP の server と tool を読む。遮断は終了コード 2 だけ。
    """

    def __init__(self) -> None:
        super().__init__("windsurf", InputSpec(session=("trajectory_id",)))

    def _parse(self, data: dict[str, Any], *, post: bool) -> ToolCall:
        action = required_str(data, "agent_action_name")
        prefix = "post_" if post else "pre_"
        base = action[len(prefix) :] if action.startswith(prefix) else ""
        if base not in WINDSURF_ACTIONS:
            raise InputError(f"未対応の操作: {action}")
        info = data.get("tool_info")
        if not isinstance(info, dict):
            raise InputError("tool_info が無い")
        tool_input = {k: v for k, v in info.items() if k not in _WINDSURF_RESULT_KEYS}
        call = ToolCall(
            product=self.product,
            session_id=required_str(data, "trajectory_id"),
            tool_name="pre_" + base,
            tool_input=tool_input,
            cwd=optional_str(info.get("cwd")),
        )
        if post:
            for key in _WINDSURF_RESULT_KEYS:
                if key in info:
                    call.tool_response = info.get(key)
                    call.has_response = True
        return call


# Claude Code と同じ名前の項目を持つ製品の入力。
_CLAUDE_LIKE = InputSpec(
    tool_use_id=("tool_use_id",),
    cwd=("cwd",),
    response=("tool_response",),
    error=("error",),
)

ADAPTERS: dict[str, Callable[[], ProductAdapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "copilot_cli": CopilotCliAdapter,
    "cursor": CursorAdapter,
    # https://code.visualstudio.com/docs/agents/reference/hooks-reference
    "vscode_copilot": lambda: PermissionDecisionAdapter("vscode_copilot", _CLAUDE_LIKE),
    # https://docs.devin.ai/desktop/cascade/hooks
    "windsurf": WindsurfAdapter,
    # https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/hooks/reference.md
    # 許可は何も書かない。decision allow の効き目は公式に書かれていない。
    "gemini_cli": lambda: DecisionAdapter(
        "gemini_cli", InputSpec(cwd=("cwd",), response=("tool_response",))
    ),
    # https://raw.githubusercontent.com/QwenLM/qwen-code/main/docs/users/features/hooks.md
    "qwen_code": lambda: PermissionDecisionAdapter("qwen_code", _CLAUDE_LIKE),
    # https://kiro.dev/docs/hooks/types/ と https://kiro.dev/docs/hooks/actions/
    "kiro": lambda: ExitCodeAdapter(
        "kiro", InputSpec(cwd=("cwd",), response=("tool_response",))
    ),
    # https://raw.githubusercontent.com/aws/amazon-q-developer-cli/main/docs/hooks.md
    # 入力にセッションの識別子が無い。
    "amazon_q_cli": lambda: ExitCodeAdapter(
        "amazon_q_cli",
        InputSpec(
            session=(),
            cwd=("cwd",),
            response=("tool_response",),
            session_from_process=True,
        ),
    ),
    # https://docs.factory.com/reference/hooks-reference
    "factory_droid": lambda: PermissionDecisionAdapter(
        "factory_droid", InputSpec(cwd=("cwd",), response=("tool_response",))
    ),
    # https://raw.githubusercontent.com/charmbracelet/crush/main/docs/hooks/README.md
    # 許可は何も書かない。decision allow は利用者の許可の確認を飛ばす。
    "crush": lambda: DecisionAdapter("crush", InputSpec(cwd=("cwd",))),
    # https://goose-docs.ai/docs/guides/context-engineering/hooks/
    # 遮断の decision は block だけを読む。許可は空の標準出力で終了コード 0。
    "goose": lambda: DecisionAdapter(
        "goose",
        InputSpec(tool_use_id=("tool_call_id",), cwd=("working_dir",)),
        deny_word="block",
    ),
    # https://docs.augmentcode.com/cli/hooks
    # メールは導入のコマンドが includeUserContext を書いたときの context.userEmail。
    "augment_code": lambda: PermissionDecisionAdapter(
        "augment_code",
        InputSpec(
            session=("conversation_id",),
            user_email=("context", "userEmail"),
            response=("tool_output",),
            error=("tool_error",),
        ),
    ),
    # https://junie.jetbrains.com/docs/junie-cli-hooks.html
    # 許可は何も書かない。決定を省いた出力は許可と書かれている。PreToolUse の入力の例は tool_name と
    # tool_input だけで、session_id は持たないことがある。
    # 許可は何も書かず終了コード 0 で返す。公式の文書は、PreToolUse の decision を省くと allow と同じで
    # 元の入力で tool が動くと書き、確認を省くとは書いていない。判断を省いたときに製品の許可の確認が
    # 省かれるのは PermissionRequest の説明で、PreToolUse には当たらない。
    "junie_cli": lambda: DecisionAdapter(
        "junie_cli", InputSpec(cwd=("cwd",), session_from_process=True)
    ),
    # https://raw.githubusercontent.com/cline/cline/main/.clinerules/hooks/README.md
    "cline": lambda: ClineAdapter(
        "cline",
        InputSpec(
            session=("taskId",),
            tool_name="toolName",
            tool_input="parameters",
            pre_container="preToolUse",
            post_container="postToolUse",
            response=("result",),
        ),
    ),
    # https://antigravity.google/docs/hooks/
    # decision は必須で、allow は次の hook か既定の方針へ委ねる。終了コードの意味は書かれていないため、
    # 遮断も標準出力の JSON と終了コード 0 で返す。
    "antigravity_cli": lambda: DecisionAdapter(
        "antigravity_cli",
        InputSpec(
            session=("conversationId",),
            tool_name="name",
            tool_input="args",
            pre_container="toolCall",
            post_container="toolCall",
            error=("error",),
        ),
        allow={"decision": "allow"},
        deny_exit_code=0,
        post_output={},
    ),
}
