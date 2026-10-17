"""コーディングエージェントの hook の設定を書き込む導入のコマンド。

管理の置き場所へ書く方式を主にする。管理の置き場所は root の権限が要り、書けないときは失敗にする。
利用者の階層へは黙って切り替えない。利用者の階層への書き込みは試用に限る。利用者とエージェントが
外せるため、監督の境界にならない。

既存の設定は壊さない。読めない設定は書き換えずに失敗にする。Argus の hook の項目だけを入れ替え、
重ねて追記しない。中身が変わるときだけ、書き戻す前に元のファイルを控える。
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from senda_argus_hooks.integrations.coding_agent_hooks import (
    MANAGED_CONFIG_PATH,
    PRODUCT_HOOK_TIMEOUT_SEC,
    PRODUCTS,
)
from senda_argus_hooks.integrations.hook_products import WINDSURF_ACTIONS

# Argus の hook の項目を見分ける目印。hook の command にこの文字列を含む項目を Argus の物とみなす。
HOOK_MARKER = "senda_argus_hooks.cli.main hook"
BACKUP_SUFFIX = ".senda-argus.bak"
# command に script の場所だけを受ける製品へ書く、包みの .sh の名前の頭。この名前も Argus のマークとする。
WRAPPER_PREFIX = "senda-argus-hook-"

SCOPES = ("managed", "user")
PLATFORMS = ("linux", "darwin")

USER_SCOPE_NOTICE = (
    "利用者の階層へ書いた。これは試用のための導入で、利用者とエージェントが設定を外せるため監督の境界にならない。"
    " 運用では --scope managed で管理の置き場所へ書く"
)


class InstallError(Exception):
    """導入できない。理由を利用者へ示して終了する。"""


@dataclass
class InstallResult:
    product: str
    scope: str
    path: str
    changed: bool
    backup: str | None
    # 書いた、または確認したファイルの全て。設定の他に包みの .sh と付随のファイルを含む。
    files: list[str] = field(default_factory=list)
    # 導入のコマンドでは済まず、利用者が製品の側で行う操作。空なら無い。
    notice: str = ""


def current_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    raise InstallError(f"未対応の OS: {sys.platform}")


@dataclass(frozen=True)
class McpSource:
    """製品が MCP サーバの定義を置くファイル 1 つ。scan-mcp が読むだけで、導入では書かない。

    paths は OS ごとの、base からの相対の場所。None は hook の設定と同じファイル (settings_path の
    全ての方式) を指す。base は home (利用者の家のディレクトリ) か cwd (コマンドを実行した
    ディレクトリだけで、親をたどらない)。key は定義の対応を置く鍵の並び。per_project は、
    key の下がプロジェクトの場所ごとの表で、各表の mcpServers に定義を置く形。
    """

    key: tuple[str, ...]
    paths: dict[str, str] | None = None
    base: str = "home"
    fmt: str = "json"
    scope: str = "user"
    per_project: bool = False


def _home_paths(rel: str) -> dict[str, str]:
    return {"linux": rel, "darwin": rel}


@dataclass(frozen=True)
class ProductInstall:
    """製品ごとの、設定の置き場所と書き方。

    managed は OS ごとの system_root からの相対の場所、user は home からの相対の場所。公式の文書に
    書かれていない方式は None にし、理由を *_refusal に書く。layout は hook の項目の置き方:
    hooks は最上位の hooks の下にイベントごとの配列、root は最上位にイベントごとの配列、named は
    最上位の Argus の名前の下にイベントの表、list は最上位の hooks の配列に項目を並べる、script は
    イベントごとの実行可能なファイル。
    """

    entries: Callable[[str, str, int], dict[str, Any]]
    layout: str = "hooks"
    managed: dict[str, str] | None = None
    managed_refusal: str = ""
    user: str | None = None
    user_refusal: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    # command に拡張子のある script の場所だけを受ける製品は、hook の command を包む .sh を書く。
    wrapper: bool = False
    # 設定と同じ場所に、無いときだけ書く付随のファイル。相対の場所と中身。
    companions: dict[str, Any] = field(default_factory=dict)
    managed_notice: str = ""
    # 利用者の階層の導入で、導入のコマンドでは済まず、利用者が製品の側で行う操作。
    user_notice: str = ""
    # 設定のファイルが無いときだけ製品が読む、別の置き場所。"方式:OS" ごとに、base (管理は system_root、
    # 利用者は home) からの相対の場所と、中身の JSON で hook を運ぶ鍵 (None はファイルの有無だけを見る)。
    # 設定のファイルを新しく作ると、ここに置かれた既存の hook が読まれなくなるため、導入しない。
    shadowed: dict[str, tuple[tuple[str, str | None], ...]] = field(
        default_factory=dict
    )
    # MCP サーバの定義の置き場所。出典は各行の上に書く。公式に書かれていない置き場所は読まず、
    # 理由を mcp_refusal に書く。mcp_platform_refusal は、その OS に対応する置き場所が 1 つも無いときだけ
    # 使う理由。
    mcp_sources: tuple[McpSource, ...] = ()
    mcp_refusal: str = ""
    mcp_platform_refusal: str = ""


_NO_MANAGED = "管理者用の置き場所は公式の文書に書かれていないため、管理の導入はできない。利用者の階層へは切り替えない"
_NAMED_KEY = "senda-argus"


def _claude_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    return {
        "PreToolUse": {
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": pre,
                    "timeout": timeout,
                    "onFailure": "block",
                }
            ],
        },
        "PostToolUse": {
            "matcher": "*",
            "hooks": [{"type": "command", "command": post, "timeout": timeout}],
        },
        # 失敗と中断で終わった実行は PostToolUse に届かない。記録が欠けると静かな欠落に見える。
        "PostToolUseFailure": {
            "matcher": "*",
            "hooks": [{"type": "command", "command": post, "timeout": timeout}],
        },
    }


def _codex_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    return {
        "PreToolUse": {
            "matcher": "*",
            "hooks": [{"type": "command", "command": pre, "timeout": timeout}],
        },
        "PostToolUse": {
            "matcher": "*",
            "hooks": [{"type": "command", "command": post, "timeout": timeout}],
        },
    }


def _copilot_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # HTTP の hook は通信の失敗で許可に倒れるため、command の hook にする。
    return {
        "preToolUse": {"type": "command", "bash": pre, "timeoutSec": timeout},
        "postToolUse": {"type": "command", "bash": post, "timeoutSec": timeout},
        "postToolUseFailure": {"type": "command", "bash": post, "timeoutSec": timeout},
    }


def _cursor_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    return {
        "preToolUse": {"command": pre, "timeout": timeout, "failClosed": True},
        "postToolUse": {"command": post, "timeout": timeout},
        "postToolUseFailure": {"command": post, "timeout": timeout},
    }


def _group(command: str, *, matcher: str | None = None, **extra: Any) -> dict[str, Any]:
    """matcher の群の中に command の hook を 1 件置いた項目。"""
    hook: dict[str, Any] = {"type": "command", "command": command, **extra}
    group: dict[str, Any] = {"hooks": [hook]}
    if matcher is not None:
        group = {"matcher": matcher, **group}
    return group


def _windsurf_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # 全ツールをまとめる hook は無いため、操作ごとの実行の前と後へ同じ command を書く。時間の項目は
    # 公式に書かれていない。
    entries: dict[str, Any] = {}
    for action in WINDSURF_ACTIONS:
        entries[f"pre_{action}"] = {"command": pre}
        entries[f"post_{action}"] = {"command": post}
    return entries


def _gemini_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # matcher は正規表現。省いたときの扱いは公式に書かれていないため、全ての tool に当たる .* を書く。
    # timeout はミリ秒。
    ms = timeout * 1000
    return {
        "BeforeTool": _group(pre, matcher=".*", name=_NAMED_KEY, timeout=ms),
        "AfterTool": _group(post, matcher=".*", name=_NAMED_KEY, timeout=ms),
    }


def _vscode_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    return {
        "PreToolUse": {"type": "command", "command": pre, "timeout": timeout},
        "PostToolUse": {"type": "command", "command": post, "timeout": timeout},
    }


def _kiro_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # matcher を省くと全ての tool に当たると公式に書かれている。
    return {
        "PreToolUse": {
            "name": f"{_NAMED_KEY}-pre",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": pre},
            "timeout": timeout,
        },
        "PostToolUse": {
            "name": f"{_NAMED_KEY}-post",
            "trigger": "PostToolUse",
            "action": {"type": "command", "command": post},
            "timeout": timeout,
        },
    }


def _factory_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # matcher を省くと全てに当たると公式に書かれている。
    return {
        "PreToolUse": _group(pre, timeout=timeout),
        "PostToolUse": _group(post, timeout=timeout),
    }


def _crush_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # 実行の後の hook は無い。matcher は正規表現で、省いたときの扱いは公式に書かれていない。
    return {
        "PreToolUse": {
            "name": _NAMED_KEY,
            "matcher": ".*",
            "command": pre,
            "timeout": timeout,
        }
    }


def _goose_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # on_failure の既定は allow で、hook の失敗で実行が続く。block を書いて失敗を遮断へ倒す。
    # "*" 単独の matcher は無効なため、matcher を省いて全ての tool に当てる。
    return {
        "PreToolUse": _group(pre, timeout=timeout, on_failure="block"),
        "PostToolUse": _group(post, timeout=timeout),
        "PostToolUseFailure": _group(post, timeout=timeout),
    }


def _augment_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # timeout はミリ秒。includeUserContext で入力に context.userEmail が載る。
    meta = {"includeUserContext": True}
    ms = timeout * 1000
    return {
        "PreToolUse": _group(pre, timeout=ms, metadata=meta),
        "PostToolUse": _group(post, timeout=ms, metadata=meta),
    }


def _junie_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # 実行の後の hook は無い。matcher を省くと全ての tool に当たると公式に書かれている。
    return {"PreToolUse": _group(pre, timeout=timeout)}


def _antigravity_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    return {
        "PreToolUse": _group(pre, matcher=".*", timeout=timeout),
        "PostToolUse": _group(post, matcher=".*", timeout=timeout),
    }


def _cline_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    # イベントの名前のファイルを置く。時間は製品の側で 30 秒。
    return {"PreToolUse": pre, "PostToolUse": post}


def _no_hook_entries(pre: str, post: str, timeout: int) -> dict[str, Any]:
    raise InstallError("hook を持たない製品")


_MCP_SERVERS = ("mcpServers",)

INSTALLS: dict[str, ProductInstall] = {
    "claude_code": ProductInstall(
        _claude_entries,
        managed={
            "linux": "etc/claude-code/managed-settings.json",
            "darwin": "Library/Application Support/ClaudeCode/managed-settings.json",
        },
        user=".claude/settings.json",
        # https://code.claude.com/docs/en/mcp.md の Local scope、Project scope、User scope
        mcp_sources=(
            McpSource(_MCP_SERVERS, _home_paths(".claude.json")),
            McpSource(("projects",), _home_paths(".claude.json"), per_project=True),
            McpSource(_MCP_SERVERS, _home_paths(".mcp.json"), base="cwd", scope="cwd"),
        ),
    ),
    "codex": ProductInstall(
        _codex_entries,
        managed_refusal=(
            "Codex の管理の置き場所 requirements.toml の場所は公式の文書に書かれていないため、管理の導入はできない。"
            " 利用者の階層へは切り替えない"
        ),
        user=".codex/hooks.json",
        # https://developers.openai.com/codex/mcp.md の Configure with config.toml
        mcp_sources=(
            McpSource(("mcp_servers",), _home_paths(".codex/config.toml"), fmt="toml"),
        ),
    ),
    "copilot_cli": ProductInstall(
        _copilot_entries,
        managed={
            "linux": "etc/github-copilot/policy.d/senda-argus.json",
            "darwin": "etc/github-copilot/policy.d/senda-argus.json",
        },
        user=".copilot/hooks/senda-argus.json",
        defaults={"version": 1},
    ),
    "cursor": ProductInstall(
        _cursor_entries,
        managed={
            "linux": "etc/cursor/hooks.json",
            "darwin": "Library/Application Support/Cursor/hooks.json",
        },
        user=".cursor/hooks.json",
        defaults={"version": 1},
        # https://cursor.com/docs/mcp.md の Using mcp.json
        mcp_sources=(
            McpSource(_MCP_SERVERS, _home_paths(".cursor/mcp.json")),
            McpSource(
                _MCP_SERVERS, _home_paths(".cursor/mcp.json"), base="cwd", scope="cwd"
            ),
        ),
    ),
    # 利用者の置き場所 ~/.copilot/hooks/*.json は Copilot CLI も読むため、書くと 1 回の呼び出しで両方の
    # 製品の hook が動く。VS Code だけが読む利用者の置き場所は公式に書かれていない。
    "vscode_copilot": ProductInstall(
        _vscode_entries,
        managed_refusal="VS Code の管理は管理者のポリシー ChatHooks で、置き場所の絶対パスは公式の文書に書かれていないため、管理の導入はできない",
        user_refusal=(
            "VS Code の利用者の置き場所 ~/.copilot/hooks/*.json は GitHub Copilot CLI も読むため、書くと両方の製品で"
            " 同じ hook が二重に動く。VS Code だけが読む置き場所は公式の文書に書かれていないため、導入しない"
        ),
        # https://github.com/microsoft/vscode-docs/blob/main/docs/agents/reference/mcp-configuration.md
        mcp_sources=(
            McpSource(
                ("servers",), _home_paths(".vscode/mcp.json"), base="cwd", scope="cwd"
            ),
        ),
        mcp_refusal=(
            "利用者のプロファイルの mcp.json の絶対の場所は公式の文書に書かれていないため読まない"
        ),
    ),
    "windsurf": ProductInstall(
        _windsurf_entries,
        managed={
            "linux": "etc/devin/hooks.json",
            "darwin": "Library/Application Support/Devin/hooks.json",
        },
        user=".codeium/windsurf/hooks.json",
        # Devin の場所にファイルが無いときだけ、旧の Windsurf の場所を読むと公式に書かれている。
        # https://docs.devin.ai/windsurf/plugins/cascade/mcp.md の mcp_config.json の節。
        # XDG_CONFIG_HOME を設定したときの場所は読まない。
        mcp_sources=(
            McpSource(_MCP_SERVERS, _home_paths(".config/devin/mcp_config.json")),
            McpSource(_MCP_SERVERS, _home_paths(".codeium/windsurf/mcp_config.json")),
        ),
        shadowed={
            "managed:linux": (("etc/windsurf/hooks.json", None),),
            "managed:darwin": (
                ("Library/Application Support/Windsurf/hooks.json", None),
            ),
        },
    ),
    "gemini_cli": ProductInstall(
        _gemini_entries,
        managed={
            "linux": "etc/gemini-cli/settings.json",
            "darwin": "Library/Application Support/GeminiCli/settings.json",
        },
        user=".gemini/settings.json",
        managed_notice=(
            "Gemini CLI のシステムの設定は環境変数 GEMINI_CLI_SYSTEM_SETTINGS_PATH で差し替えられ、hooks で管理者側が"
            " 勝つとは公式の文書に書かれていない"
        ),
        # https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md
        mcp_sources=(McpSource(_MCP_SERVERS),),
    ),
    "qwen_code": ProductInstall(
        _codex_entries,
        managed_refusal="Qwen Code のシステムの設定の絶対パスは公式の文書に書かれていないため、管理の導入はできない",
        user_refusal="Qwen Code の利用者の設定の絶対パスは公式の文書に書かれていないため、導入しない",
    ),
    "kiro": ProductInstall(
        _kiro_entries,
        layout="list",
        managed_refusal=_NO_MANAGED,
        user=".kiro/hooks/senda-argus.json",
        defaults={"version": "v1"},
    ),
    "amazon_q_cli": ProductInstall(
        _codex_entries,
        managed_refusal="Amazon Q Developer CLI の agent の設定の置き場所と組織の管理は公式の文書に書かれていないため、管理の導入はできない",
        user_refusal="Amazon Q Developer CLI の agent の設定の置き場所は公式の文書に書かれていないため、導入しない",
    ),
    "factory_droid": ProductInstall(
        _factory_entries,
        layout="root",
        managed_refusal="Factory の組織の管理の設定の絶対パスは公式の文書に書かれていないため、管理の導入はできない",
        user=".factory/hooks.json",
        # hooks.json が無いときだけ settings.json の hooks を読み、旧の hooks/hooks.json も読むと公式に
        # 書かれている。
        shadowed={
            f"user:{platform}": (
                (".factory/settings.json", "hooks"),
                (".factory/hooks/hooks.json", None),
            )
            for platform in ("linux", "darwin")
        },
    ),
    "crush": ProductInstall(
        _crush_entries,
        managed_refusal=_NO_MANAGED,
        user=".config/crush/crush.json",
        # https://github.com/charmbracelet/crush の README の MCP の節
        mcp_sources=(McpSource(("mcp",)),),
    ),
    "goose": ProductInstall(
        _goose_entries,
        managed_refusal=_NO_MANAGED,
        user=".agents/plugins/senda-argus/hooks/hooks.json",
        companions={
            "../plugin.json": {
                "name": _NAMED_KEY,
                "version": "0.1.0",
                "description": "Senda-Argus pre-execution check",
            }
        },
    ),
    "augment_code": ProductInstall(
        _augment_entries,
        managed={
            "linux": "etc/augment/settings.json",
            "darwin": "etc/augment/settings.json",
        },
        user=".augment/settings.json",
        wrapper=True,
    ),
    "junie_cli": ProductInstall(
        _junie_entries,
        managed_refusal=_NO_MANAGED,
        user=".junie/config.json",
    ),
    "cline": ProductInstall(
        _cline_entries,
        layout="script",
        managed_refusal=_NO_MANAGED,
        user="Documents/Cline/Hooks/PreToolUse",
        # Enable Hooks は Cline の設定の画面のチェックボックスで、ファイルやコマンドから設定する方法は
        # 公式に書かれていない。入れないと hook は実行されず、判定も記録も起きない。
        user_notice=(
            "Cline の設定の画面の Feature Settings で、Enable Hooks を有効にする必要がある。"
            " 有効にするまで hook は実行されず、Argus の判定と記録は起きない。"
        ),
        # https://github.com/cline/cline/blob/main/docs/mcp/mcp-overview.mdx の Manual config
        mcp_sources=(
            McpSource(
                _MCP_SERVERS,
                _home_paths(".cline/data/settings/cline_mcp_settings.json"),
            ),
        ),
        mcp_refusal=(
            "IDE の拡張機能の設定の場所は公式の文書に絶対の場所が書かれていないため読まない"
        ),
    ),
    "antigravity_cli": ProductInstall(
        _antigravity_entries,
        layout="named",
        managed_refusal=_NO_MANAGED,
        user=".gemini/config/hooks.json",
    ),
    # hook を持たず、MCP サーバの定義だけを読む製品。導入のコマンドの対象 PRODUCTS には入らない。
    # https://modelcontextprotocol.io/docs/develop/connect-local-servers.md
    "claude_desktop": ProductInstall(
        _no_hook_entries,
        managed_refusal="hook を持たない製品",
        user_refusal="hook を持たない製品",
        mcp_sources=(
            McpSource(
                _MCP_SERVERS,
                {
                    "darwin": "Library/Application Support/Claude/claude_desktop_config.json"
                },
            ),
        ),
        mcp_platform_refusal="この OS の置き場所は公式の文書に書かれていないため読まない",
    ),
}


def settings_path(
    product: str, scope: str, platform: str, *, home: Path, system_root: Path
) -> Path:
    """製品と方式ごとの設定の置き場所。system_root と home はテストで付け替える。"""
    spec = INSTALLS.get(product)
    if spec is None:
        raise InstallError(f"未対応の組み合わせ: {product} {scope}")
    if scope == "managed":
        if spec.managed is None:
            raise InstallError(spec.managed_refusal or _NO_MANAGED)
        rel = spec.managed.get(platform)
        if rel is None:
            raise InstallError(f"未対応の組み合わせ: {product} {scope} {platform}")
        return system_root / rel
    if scope == "user":
        if spec.user is None:
            raise InstallError(spec.user_refusal)
        return home / spec.user
    raise InstallError(f"未対応の組み合わせ: {product} {scope}")


def _isolation_flag(scope: str) -> str:
    """hook の python に付ける、読み込み先を絞る引数。

    作業ディレクトリを読み込み先から外さないと、エージェントがプロジェクトに同じ名前のモジュールを
    置くだけで hook の代わりに動く。管理の導入は -I で、作業ディレクトリと PYTHONPATH と利用者の
    site-packages を外す。利用者の階層の導入は pip install --user のパッケージを読めなければ動かないので、
    利用者の site-packages を残す -P を使う。-P は Python 3.11 以降で、それより前は -I にする。
    """
    if scope == "user" and sys.version_info >= (3, 11):
        return "-P"
    return "-I"


def hook_command(
    product: str, phase: str, scope: str, *, python: str | None = None
) -> str:
    """製品の hook に書く command。管理の導入では、管理の設定ファイルだけを読ませる。"""
    parts = [
        python or sys.executable,
        _isolation_flag(scope),
        "-m",
        "senda_argus_hooks.cli.main",
        "hook",
        product,
        phase,
    ]
    if scope == "managed":
        parts += ["--config", str(MANAGED_CONFIG_PATH)]
    return " ".join(shlex.quote(p) for p in parts)


def _contains_ours(value: Any) -> bool:
    """入れ子の定義のどこかの文字列が Argus の hook の command か。"""
    if isinstance(value, str):
        return HOOK_MARKER in value or WRAPPER_PREFIX in value
    if isinstance(value, dict):
        return any(_contains_ours(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_ours(v) for v in value)
    return False


def _is_ours(entry: Any) -> bool:
    """Argus の hook の項目か。項目の command と、Kiro の action の command と、包みの .sh の名前を見る。"""
    if isinstance(entry, dict):
        values = [entry.get("command"), entry.get("bash")]
        action = entry.get("action")
        if isinstance(action, dict):
            values.append(action.get("command"))
        for value in values:
            if isinstance(value, str) and (
                HOOK_MARKER in value or WRAPPER_PREFIX in value
            ):
                return True
    return False


def _without_ours(current: list[Any]) -> list[Any]:
    """Argus の項目だけを除く。入れ子の hooks の群は、群の中の Argus の項目だけを除き、空になった群だけを消す。

    群ごと消すと、同じ群に並べた組織の別の hook が導入し直しで黙って外れる。
    """
    kept: list[Any] = []
    for item in current:
        if _is_ours(item):
            continue
        inner = item.get("hooks") if isinstance(item, dict) else None
        if isinstance(inner, list) and any(_is_ours(h) for h in inner):
            rest = [h for h in inner if not _is_ours(h)]
            if not rest:
                continue
            item = {**item, "hooks": rest}
        kept.append(item)
    return kept


def _hook_entries(
    product: str,
    scope: str,
    python: str | None,
    commands: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """製品の設定へ書く hook の項目。イベント名ごとに 1 件。"""
    spec = INSTALLS.get(product)
    if spec is None:
        raise InstallError(f"未対応の製品: {product}")
    pre, post = commands or (
        hook_command(product, "pre", scope, python=python),
        hook_command(product, "post", scope, python=python),
    )
    return spec.entries(pre, post, PRODUCT_HOOK_TIMEOUT_SEC)


def _merge_events(target: dict[str, Any], entries: dict[str, Any], where: str) -> None:
    for event, entry in entries.items():
        current = target.get(event, [])
        if not isinstance(current, list):
            raise InstallError(
                f"既存の設定の {where}{event} が配列でないため書き換えない"
            )
        target[event] = _without_ours(current) + [entry]


def merge_settings(
    existing: dict[str, Any],
    product: str,
    scope: str,
    python: str | None = None,
    *,
    commands: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """既存の設定へ Argus の hook を入れる。他の項目と他の hook は保つ。"""
    spec = INSTALLS.get(product)
    if spec is None or spec.layout == "script":
        raise InstallError(f"設定ファイルへ書く製品でない: {product}")
    merged = json.loads(json.dumps(existing))
    for key, value in spec.defaults.items():
        merged.setdefault(key, value)
    entries = _hook_entries(product, scope, python, commands)
    if spec.layout == "root":
        _merge_events(merged, entries, "")
    elif spec.layout == "named":
        current = merged.get(_NAMED_KEY)
        if current is not None and not isinstance(current, dict):
            raise InstallError(
                f"既存の設定の {_NAMED_KEY} がオブジェクトでないため書き換えない"
            )
        # 名前は利用者が自由に付けるので、同じ名前が別の hook のこともある。Argus の hook を含まない
        # 既存の定義は、丸ごと入れ替えると消えてしまうため、書き換えない。
        if current and not _contains_ours(current):
            raise InstallError(
                f"既存の設定の {_NAMED_KEY} は Argus の hook を含まないため書き換えない。"
                " 別の名前に変えてから導入し直す"
            )
        # Argus の名前の下は Argus だけが書く。導入し直しで丸ごと入れ替える。
        merged[_NAMED_KEY] = {event: [entry] for event, entry in entries.items()}
    elif spec.layout == "list":
        hooks = merged.setdefault("hooks", [])
        if not isinstance(hooks, list):
            raise InstallError("既存の設定の hooks が配列でないため書き換えない")
        merged["hooks"] = _without_ours(hooks) + list(entries.values())
    else:
        hooks = merged.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise InstallError(
                "既存の設定の hooks がオブジェクトでないため書き換えない"
            )
        _merge_events(hooks, entries, "hooks.")
    return merged


def _check_root_owned(path: Path) -> None:
    st = os.stat(path)
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise InstallError(
            f"{path} が root の所有でないか、グループか他者に書き込みの権限がある。"
            " 管理の導入の hook は利用者が書き換えられる python とパッケージで動かさない"
        )


def _check_managed_runtime(python: str) -> None:
    """管理の hook が動かす python とパッケージが、利用者に書き換えられないことを確かめる。"""
    import senda_argus_hooks

    package_dir = Path(senda_argus_hooks.__file__).resolve().parent
    for path in (Path(os.path.realpath(python)), package_dir, package_dir.parent):
        _check_root_owned(path)


# 試験で差し替えるための参照。
managed_runtime_check = _check_managed_runtime
managed_dir_check = _check_root_owned


def _read_existing(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except PermissionError as exc:
        raise InstallError(f"{path} を読めない。権限が足りない") from exc
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(f"{path} が JSON として読めないため書き換えない") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} が JSON のオブジェクトでないため書き換えない")
    return data, text


def _write_atomic(path: Path, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".senda-argus-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def wrapper_script(command: str) -> str:
    """hook の command を実行する sh の中身。Argus のマークを含む。"""
    return f"#!/bin/sh\nexec {command}\n"


@dataclass
class _PlannedFile:
    path: Path
    text: str
    mode: int
    original: str | None
    main: bool = False


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (PermissionError, UnicodeDecodeError) as exc:
        raise InstallError(f"{path} を読めない") from exc


def _plan_scripts(
    product: str, scope: str, python: str | None, paths: dict[str, Path]
) -> tuple[list[_PlannedFile], tuple[str, str]]:
    """包みの .sh か、Cline のイベントのファイルを用意する。他者のファイルは上書きしない。"""
    mode = 0o755 if scope == "managed" else 0o700
    planned: list[_PlannedFile] = []
    for phase in ("pre", "post"):
        path = paths[phase]
        text = wrapper_script(hook_command(product, phase, scope, python=python))
        original = _read_text(path)
        if original is not None and HOOK_MARKER not in original:
            raise InstallError(
                f"{path} に Argus でない hook があるため書き換えない。中身を移してから、もう一度導入する"
            )
        planned.append(_PlannedFile(path, text, mode, original, main=phase == "pre"))
    return planned, (str(paths["pre"]), str(paths["post"]))


def _config_mode(scope: str, path: Path, original: str | None) -> int:
    # 管理の置き場所は他者に書かせない。Copilot CLI は root の所有で書き込みの無い物だけを読む。
    if scope == "managed":
        return 0o644
    if original is not None:
        return os.stat(path).st_mode & 0o777
    return 0o600


def _check_shadowed(
    spec: ProductInstall, scope: str, platform: str, base: Path
) -> None:
    for rel, key in spec.shadowed.get(f"{scope}:{platform}", ()):
        other = base / rel
        text = _read_text(other)
        if text is None:
            continue
        if key is not None:
            try:
                data = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                data = {key: None}
            if not isinstance(data, dict) or key not in data:
                continue
        raise InstallError(
            f"{other} に既存の hook の設定がある。製品は新しく作る設定のファイルがあるとこれを読まなくなるため、"
            "書かない。中身を設定のファイルへ移してから、もう一度導入する"
        )


def _plan(
    product: str,
    scope: str,
    path: Path,
    python: str | None,
    *,
    platform: str = "linux",
    base: Path | None = None,
) -> list[_PlannedFile]:
    """書く物を全部決める。他者の設定を壊す書き込みがあれば、何も書く前に失敗する。"""
    spec = INSTALLS[product]
    if base is not None and not path.exists():
        _check_shadowed(spec, scope, platform, base)
    if spec.layout == "script":
        planned, _ = _plan_scripts(
            product,
            scope,
            python,
            {"pre": path, "post": path.with_name("PostToolUse")},
        )
        return planned
    planned: list[_PlannedFile] = []
    commands: tuple[str, str] | None = None
    if spec.wrapper:
        hooks_dir = path.parent / "hooks"
        # 管理の導入の包みは root の権限で動く。置き場所を利用者が書けると、包みを差し替えられる。
        if scope == "managed" and hooks_dir.exists():
            managed_dir_check(hooks_dir)
        planned, commands = _plan_scripts(
            product,
            scope,
            python,
            {
                phase: hooks_dir / f"{WRAPPER_PREFIX}{phase}.sh"
                for phase in ("pre", "post")
            },
        )
        for item in planned:
            item.main = False
    existing, original = _read_existing(path)
    merged = merge_settings(existing, product, scope, python, commands=commands)
    # 既に同じ内容なら書かない。整形の違いだけで書き戻して控えを増やさない。
    text = (
        original
        if original is not None and existing == merged
        else json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    )
    planned.append(
        _PlannedFile(path, text, _config_mode(scope, path, original), original, True)
    )
    for rel, content in spec.companions.items():
        companion = Path(os.path.normpath(path.parent / rel))
        current = _read_text(companion)
        # 付随のファイルは無いときだけ書く。利用者が書き換えた物は保つ。
        text = (
            current
            if current is not None
            else json.dumps(content, ensure_ascii=False, indent=2) + "\n"
        )
        planned.append(
            _PlannedFile(
                companion, text, _config_mode(scope, companion, current), current
            )
        )
    return planned


def _write_planned(item: _PlannedFile, scope: str) -> str | None:
    backup: str | None = None
    try:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        if item.original is not None:
            backup_path = item.path.with_name(
                f"{item.path.name}{BACKUP_SUFFIX}-{time.strftime('%Y%m%d%H%M%S')}"
            )
            shutil.copy2(item.path, backup_path)
            backup = str(backup_path)
        _write_atomic(item.path, item.text, item.mode)
    except PermissionError as exc:
        if scope == "managed":
            raise InstallError(
                f"{item.path} へ書けない。管理の置き場所への書き込みは root の権限が要る。"
                " 利用者の階層へは切り替えないため、sudo で実行し直す"
            ) from exc
        raise InstallError(f"{item.path} へ書けない。権限が足りない") from exc
    return backup


def install_product(
    product: str,
    scope: str,
    *,
    platform: str | None = None,
    home: Path | None = None,
    system_root: Path | None = None,
    python: str | None = None,
    dry_run: bool = False,
) -> InstallResult:
    if product not in PRODUCTS:
        raise InstallError(f"未対応の製品: {product}")
    if scope not in SCOPES:
        raise InstallError(f"未対応の方式: {scope}")
    platform = platform or current_platform()
    if platform not in PLATFORMS:
        raise InstallError(f"未対応の OS: {platform}")
    path = settings_path(
        product,
        scope,
        platform,
        home=home or Path.home(),
        system_root=system_root or Path("/"),
    )
    if scope == "managed":
        managed_runtime_check(python or sys.executable)
    planned = _plan(
        product,
        scope,
        path,
        python,
        platform=platform,
        base=(system_root or Path("/"))
        if scope == "managed"
        else (home or Path.home()),
    )
    changes = [item for item in planned if item.original != item.text]
    files = [str(item.path) for item in planned]
    if not changes:
        return InstallResult(product, scope, str(path), False, None, files)
    if dry_run:
        return InstallResult(product, scope, str(path), True, None, files)
    backup: str | None = None
    # 包みの .sh を先に書き、設定はその後に書く。設定が指す物が無い状態を作らない。
    for item in changes:
        written = _write_planned(item, scope)
        if item.main:
            backup = written
    return InstallResult(product, scope, str(path), True, backup, files)


def preflight_install(products: list[str], scope: str) -> None:
    """書き込みの前に、製品と方式の組が全て対応していることを確認する。"""
    platform = current_platform()
    if platform not in PLATFORMS:
        raise InstallError(f"未対応の OS: {platform}")
    for product in products:
        if product not in PRODUCTS:
            raise InstallError(f"未対応の製品: {product}")
        if scope not in SCOPES:
            raise InstallError(f"未対応の方式: {scope}")
        settings_path(product, scope, platform, home=Path.home(), system_root=Path("/"))


def supported_products(scope: str) -> tuple[list[str], dict[str, str]]:
    """指定の方式で導入できる製品と、できない製品の理由を返す。

    --product all は、この一覧の製品だけを導入する。製品を名指しした導入は、1 つでも対応しない組み合わせが
    あれば何も書かずに失敗する。all は名指しでないので、対応しない製品を理由つきで飛ばす。
    """
    supported: list[str] = []
    skipped: dict[str, str] = {}
    for product in PRODUCTS:
        try:
            preflight_install([product], scope)
        except InstallError as exc:
            skipped[product] = str(exc)
        else:
            supported.append(product)
    return supported, skipped


def run_install(
    products: list[str], scope: str, *, dry_run: bool = False, stream=None
) -> list[InstallResult]:
    out = stream if stream is not None else sys.stderr
    # 書き込みの前に、全ての製品と方式の組を検証する。途中で失敗すると、先の製品だけ書かれて
    # 残りが書かれない中途半端な状態になる。
    preflight_install(products, scope)
    # 既存の設定の読めなさや他者の hook との衝突は、書く物を決める段で分かる。全ての製品で書く物を
    # 決め終えてから書く。
    if not dry_run:
        for product in products:
            install_product(product, scope, dry_run=True)
    results = [install_product(product, scope, dry_run=dry_run) for product in products]
    if scope == "user":
        print(USER_SCOPE_NOTICE, file=out)
        # 導入のコマンドでは済まない操作は、成功の表示と一緒に出す。出さないと、導入が済んだと読まれて
        # hook が実行されない状態に気付けない。
        for product, result in zip(products, results, strict=True):
            notice = INSTALLS[product].user_notice
            if notice:
                if isinstance(result, InstallResult):
                    result.notice = notice
                print(f"{product}: {notice}", file=out)
        return results
    print(
        f"管理の置き場所へ書いた。hook は {MANAGED_CONFIG_PATH} だけから送信先と鍵と fail_open を読む。"
        " このファイルは root の所有にし、グループと他者に書き込みの権限を与えない",
        file=out,
    )
    for product in products:
        notice = INSTALLS[product].managed_notice
        if notice:
            print(notice, file=out)
    return results
