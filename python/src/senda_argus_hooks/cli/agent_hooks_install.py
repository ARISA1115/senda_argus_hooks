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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from senda_argus_hooks.integrations.coding_agent_hooks import (
    MANAGED_CONFIG_PATH,
    PRODUCT_HOOK_TIMEOUT_SEC,
    PRODUCTS,
)

# Argus の hook の項目を見分ける目印。hook の command にこの文字列を含む項目を Argus の物とみなす。
HOOK_MARKER = "senda_argus_hooks.cli.main hook"
BACKUP_SUFFIX = ".senda-argus.bak"

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


def current_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    raise InstallError(f"未対応の OS: {sys.platform}")


def settings_path(
    product: str, scope: str, platform: str, *, home: Path, system_root: Path
) -> Path:
    """製品と方式ごとの設定の置き場所。system_root と home は試験で付け替える。"""
    if scope == "managed":
        if product == "claude_code":
            rel = (
                "Library/Application Support/ClaudeCode/managed-settings.json"
                if platform == "darwin"
                else "etc/claude-code/managed-settings.json"
            )
            return system_root / rel
        if product == "cursor":
            rel = (
                "Library/Application Support/Cursor/hooks.json"
                if platform == "darwin"
                else "etc/cursor/hooks.json"
            )
            return system_root / rel
        if product == "copilot_cli":
            return system_root / "etc/github-copilot/policy.d/senda-argus.json"
        if product == "codex":
            raise InstallError(
                "Codex の管理の置き場所 requirements.toml の場所は公式の文書に書かれていないため、管理の導入はできない。"
                " 利用者の階層へは切り替えない"
            )
    if scope == "user":
        if product == "claude_code":
            return home / ".claude/settings.json"
        if product == "codex":
            return home / ".codex/hooks.json"
        if product == "copilot_cli":
            return home / ".copilot/hooks/senda-argus.json"
        if product == "cursor":
            return home / ".cursor/hooks.json"
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


def _is_ours(entry: Any) -> bool:
    """Argus の hook の項目か。項目の command だけを見る。"""
    if isinstance(entry, dict):
        for key in ("command", "bash"):
            value = entry.get(key)
            if isinstance(value, str) and HOOK_MARKER in value:
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
    product: str, scope: str, python: str | None
) -> dict[str, dict[str, Any]]:
    """製品の設定へ書く hook の項目。イベント名ごとに 1 件。"""
    pre = hook_command(product, "pre", scope, python=python)
    post = hook_command(product, "post", scope, python=python)
    timeout = PRODUCT_HOOK_TIMEOUT_SEC
    if product == "claude_code":
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
    if product == "codex":
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
    if product == "copilot_cli":
        # HTTP の hook は通信の失敗で許可に倒れるため、command の hook にする。
        return {
            "preToolUse": {"type": "command", "bash": pre, "timeoutSec": timeout},
            "postToolUse": {"type": "command", "bash": post, "timeoutSec": timeout},
            "postToolUseFailure": {
                "type": "command",
                "bash": post,
                "timeoutSec": timeout,
            },
        }
    if product == "cursor":
        return {
            "preToolUse": {"command": pre, "timeout": timeout, "failClosed": True},
            "postToolUse": {"command": post, "timeout": timeout},
            "postToolUseFailure": {"command": post, "timeout": timeout},
        }
    raise InstallError(f"未対応の製品: {product}")


def merge_settings(
    existing: dict[str, Any], product: str, scope: str, python: str | None = None
) -> dict[str, Any]:
    """既存の設定へ Argus の hook を入れる。他の項目と他の hook は保つ。"""
    merged = json.loads(json.dumps(existing))
    if product in ("copilot_cli", "cursor"):
        merged.setdefault("version", 1)
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("既存の設定の hooks がオブジェクトでないため書き換えない")
    for event, entry in _hook_entries(product, scope, python).items():
        current = hooks.get(event, [])
        if not isinstance(current, list):
            raise InstallError(
                f"既存の設定の hooks.{event} が配列でないため書き換えない"
            )
        hooks[event] = _without_ours(current) + [entry]
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
    existing, original_text = _read_existing(path)
    merged = merge_settings(existing, product, scope, python)
    new_text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    # 既に同じ内容なら書かない。整形の違いだけで書き戻して控えを増やさない。
    if original_text is not None and existing == merged:
        return InstallResult(product, scope, str(path), changed=False, backup=None)
    if dry_run:
        return InstallResult(product, scope, str(path), changed=True, backup=None)
    backup: str | None = None
    # 管理の置き場所は他者に書かせない。Copilot CLI は root の所有で書き込みの無い物だけを読む。
    mode = 0o644 if scope == "managed" else 0o600
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if original_text is not None:
            backup_path = path.with_name(
                f"{path.name}{BACKUP_SUFFIX}-{time.strftime('%Y%m%d%H%M%S')}"
            )
            shutil.copy2(path, backup_path)
            backup = str(backup_path)
            mode = os.stat(path).st_mode & 0o777 if scope == "user" else mode
        _write_atomic(path, new_text, mode)
    except PermissionError as exc:
        if scope == "managed":
            raise InstallError(
                f"{path} へ書けない。管理の置き場所への書き込みは root の権限が要る。"
                " 利用者の階層へは切り替えないため、sudo で実行し直す"
            ) from exc
        raise InstallError(f"{path} へ書けない。権限が足りない") from exc
    return InstallResult(product, scope, str(path), changed=True, backup=backup)


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


def run_install(
    products: list[str], scope: str, *, dry_run: bool = False, stream=None
) -> list[InstallResult]:
    out = stream if stream is not None else sys.stderr
    # 書き込みの前に、全ての製品と方式の組を検証する。途中で失敗すると、先の製品だけ書かれて
    # 残りが書かれない中途半端な状態になる。
    preflight_install(products, scope)
    results = [install_product(product, scope, dry_run=dry_run) for product in products]
    if scope == "user":
        print(USER_SCOPE_NOTICE, file=out)
    else:
        print(
            f"管理の置き場所へ書いた。hook は {MANAGED_CONFIG_PATH} だけから送信先と鍵と fail_open を読む。"
            " このファイルは root の所有にし、グループと他者に書き込みの権限を与えない",
            file=out,
        )
    return results
