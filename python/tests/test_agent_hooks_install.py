from __future__ import annotations

import json
import os

import pytest

from senda_argus_hooks.cli import agent_hooks_install as inst
from senda_argus_hooks.cli.agent_hooks_install import (
    HOOK_MARKER,
    InstallError,
    install_product,
)

PY = "/opt/argus/bin/python"


@pytest.fixture(autouse=True)
def _runtime_owned_by_root(monkeypatch):
    # 試験の python とパッケージは root の所有でない。所有の検査は専用の試験で見る。
    monkeypatch.setattr(inst, "managed_runtime_check", lambda python: None)


def _install(product, scope, tmp_path, platform="linux", **kw):
    return install_product(
        product,
        scope,
        platform=platform,
        home=tmp_path / "home",
        system_root=tmp_path / "root",
        python=PY,
        **kw,
    )


def _read(result):
    return json.loads(open(result.path, encoding="utf-8").read())


@pytest.mark.parametrize(
    ("product", "platform", "rel"),
    [
        ("claude_code", "linux", "root/etc/claude-code/managed-settings.json"),
        (
            "claude_code",
            "darwin",
            "root/Library/Application Support/ClaudeCode/managed-settings.json",
        ),
        ("cursor", "linux", "root/etc/cursor/hooks.json"),
        ("cursor", "darwin", "root/Library/Application Support/Cursor/hooks.json"),
        ("copilot_cli", "linux", "root/etc/github-copilot/policy.d/senda-argus.json"),
        ("copilot_cli", "darwin", "root/etc/github-copilot/policy.d/senda-argus.json"),
    ],
)
def test_managed_paths_per_os(tmp_path, product, platform, rel):
    result = _install(product, "managed", tmp_path, platform=platform)
    assert result.path == str(tmp_path / rel)
    assert result.changed is True
    # 管理の置き場所はグループと他者に書かせない。
    assert os.stat(result.path).st_mode & 0o022 == 0


def test_claude_code_managed_shape(tmp_path):
    data = _read(_install("claude_code", "managed", tmp_path))
    pre = data["hooks"]["PreToolUse"]
    assert len(pre) == 1
    hook = pre[0]["hooks"][0]
    assert pre[0]["matcher"] == "*"
    assert hook["type"] == "command"
    assert hook["onFailure"] == "block"
    assert hook["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    assert (
        hook["command"]
        == f"{PY} -I -m senda_argus_hooks.cli.main hook claude_code pre --config /etc/senda-argus/hooks.env"
    )
    post = data["hooks"]["PostToolUse"][0]["hooks"][0]
    assert post["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    assert post["command"].endswith(
        "hook claude_code post --config /etc/senda-argus/hooks.env"
    )


def test_cursor_managed_shape(tmp_path):
    data = _read(_install("cursor", "managed", tmp_path))
    assert data["version"] == 1
    pre = data["hooks"]["preToolUse"]
    assert pre == [
        {
            "command": f"{PY} -I -m senda_argus_hooks.cli.main hook cursor pre --config /etc/senda-argus/hooks.env",
            "timeout": inst.PRODUCT_HOOK_TIMEOUT_SEC,
            "failClosed": True,
        }
    ]
    assert data["hooks"]["postToolUse"][0]["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    assert "postToolUseFailure" in data["hooks"]


def test_copilot_managed_shape_is_command_not_http(tmp_path):
    data = _read(_install("copilot_cli", "managed", tmp_path))
    assert data["version"] == 1
    pre = data["hooks"]["preToolUse"][0]
    assert pre["type"] == "command"
    assert pre["timeoutSec"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    assert "url" not in pre
    assert pre["bash"].endswith(
        "hook copilot_cli pre --config /etc/senda-argus/hooks.env"
    )
    assert (
        data["hooks"]["postToolUse"][0]["timeoutSec"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    )


def test_internal_limit_is_shorter_than_written_product_timeout():
    from senda_argus_hooks.integrations import coding_agent_hooks as hooks

    for product in hooks.PRODUCTS:
        assert hooks.DEFAULT_WAIT_LIMIT_SEC < hooks.PRODUCT_TIMEOUT_SEC[product]
        assert hooks.PRODUCT_TIMEOUT_SEC[product] <= inst.PRODUCT_HOOK_TIMEOUT_SEC


def test_codex_user_shape_and_managed_refused(tmp_path):
    result = _install("codex", "user", tmp_path)
    assert result.path == str(tmp_path / "home/.codex/hooks.json")
    data = _read(result)
    pre = data["hooks"]["PreToolUse"][0]["hooks"][0]
    assert pre["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    # 試用の導入では管理の設定ファイルを渡さない。
    assert "--config" not in pre["command"]
    with pytest.raises(InstallError, match="requirements.toml"):
        _install("codex", "managed", tmp_path)
    assert list((tmp_path / "home/.codex").glob("hooks.json.senda-argus.bak*")) == []
    assert not (tmp_path / "root/etc/codex").exists()


@pytest.mark.parametrize("product", ["claude_code", "codex", "copilot_cli", "cursor"])
def test_user_paths(tmp_path, product):
    expected = {
        "claude_code": "home/.claude/settings.json",
        "codex": "home/.codex/hooks.json",
        "copilot_cli": "home/.copilot/hooks/senda-argus.json",
        "cursor": "home/.cursor/hooks.json",
    }[product]
    result = _install(product, "user", tmp_path)
    assert result.path == str(tmp_path / expected)


@pytest.mark.parametrize("product", ["claude_code", "cursor", "copilot_cli"])
def test_install_is_idempotent(tmp_path, product):
    first = _install(product, "managed", tmp_path)
    before = open(first.path, encoding="utf-8").read()
    second = _install(product, "managed", tmp_path)
    assert second.changed is False
    assert second.backup is None
    assert open(first.path, encoding="utf-8").read() == before
    text = before
    assert text.count(HOOK_MARKER) == text.count(
        "hook " + product + " pre"
    ) + text.count("hook " + product + " post")
    for entries in json.loads(before)["hooks"].values():
        assert len(entries) == 1


def test_existing_claude_settings_are_kept_and_backed_up(tmp_path):
    path = tmp_path / "root/etc/claude-code/managed-settings.json"
    path.parent.mkdir(parents=True)
    existing = {
        "permissions": {"deny": ["Bash(curl:*)"]},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": "/usr/local/bin/lint"}],
                }
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": "/usr/local/bin/notify"}]}
            ],
        },
    }
    path.write_text(json.dumps(existing))
    result = _install("claude_code", "managed", tmp_path)
    data = _read(result)
    assert data["permissions"] == existing["permissions"]
    assert data["hooks"]["Stop"] == existing["hooks"]["Stop"]
    assert data["hooks"]["PreToolUse"][0] == existing["hooks"]["PreToolUse"][0]
    assert len(data["hooks"]["PreToolUse"]) == 2
    assert result.backup is not None
    assert json.loads(open(result.backup, encoding="utf-8").read()) == existing


def test_reinstall_replaces_old_argus_entry_instead_of_appending(tmp_path):
    path = tmp_path / "root/etc/cursor/hooks.json"
    path.parent.mkdir(parents=True)
    old = {
        "version": 1,
        "hooks": {
            "preToolUse": [
                {
                    "command": "/old/python -m senda_argus_hooks.cli.main hook cursor pre",
                    "timeout": 5,
                },
                {"command": "./other.sh"},
            ]
        },
    }
    path.write_text(json.dumps(old))
    data = _read(_install("cursor", "managed", tmp_path))
    pre = data["hooks"]["preToolUse"]
    assert {"command": "./other.sh"} in pre
    ours = [e for e in pre if HOOK_MARKER in e["command"]]
    assert len(ours) == 1
    assert ours[0]["failClosed"] is True


def test_unreadable_existing_settings_are_not_overwritten(tmp_path):
    path = tmp_path / "home/.cursor/hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ broken")
    with pytest.raises(InstallError):
        _install("cursor", "user", tmp_path)
    assert path.read_text() == "{ broken"


def test_managed_without_permission_fails_and_does_not_fall_back_to_user(tmp_path):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    os.chmod(root / "etc", 0o555)
    try:
        if os.access(root / "etc", os.W_OK):
            pytest.skip("root で動いているため権限の失敗を作れない")
        with pytest.raises(InstallError, match="root の権限"):
            _install("claude_code", "managed", tmp_path)
    finally:
        os.chmod(root / "etc", 0o755)
    assert not (tmp_path / "home/.claude/settings.json").exists()


def test_user_scope_prints_trial_notice(tmp_path, monkeypatch):
    import io

    calls = []
    monkeypatch.setattr(
        inst,
        "install_product",
        lambda product, scope, dry_run=False: calls.append((product, scope)),
    )
    stream = io.StringIO()
    inst.run_install(["cursor"], "user", stream=stream)
    assert "試用" in stream.getvalue()
    stream = io.StringIO()
    inst.run_install(["cursor"], "managed", stream=stream)
    assert "試用" not in stream.getvalue()
    assert calls == [("cursor", "user"), ("cursor", "managed")]


def test_cli_hooks_install_managed_failure_exit_code(tmp_path, monkeypatch, capsys):
    from senda_argus_hooks.cli.main import main

    def refuse(product, scope, dry_run=False):
        raise InstallError("root の権限が要る")

    monkeypatch.setattr(inst, "install_product", refuse)
    assert main(["hooks-install", "--product", "cursor"]) != 0
    assert "root の権限" in capsys.readouterr().err


def test_reinstall_keeps_other_hooks_in_the_same_group(tmp_path):
    path = tmp_path / "root/etc/claude-code/managed-settings.json"
    path.parent.mkdir(parents=True)
    existing = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "/usr/local/bin/dlp-guard"},
                        {
                            "type": "command",
                            "command": "/old/py -m senda_argus_hooks.cli.main hook claude_code pre",
                        },
                    ],
                }
            ]
        }
    }
    path.write_text(json.dumps(existing))
    data = _read(_install("claude_code", "managed", tmp_path))
    groups = data["hooks"]["PreToolUse"]
    assert groups[0] == {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "/usr/local/bin/dlp-guard"}],
    }
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert sum(HOOK_MARKER in c for c in commands) == 1


def test_managed_refuses_python_or_package_writable_by_user(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "managed_runtime_check", inst._check_managed_runtime)
    # 試験の python とパッケージは利用者の所有で、管理の導入を拒む。
    with pytest.raises(InstallError, match="root の所有でない"):
        install_product(
            "cursor",
            "managed",
            platform="linux",
            home=tmp_path / "home",
            system_root=tmp_path / "root",
        )
    assert not (tmp_path / "root/etc/cursor/hooks.json").exists()
    # 利用者の階層の試用では検査しない。
    install_product(
        "cursor",
        "user",
        platform="linux",
        home=tmp_path / "home",
        system_root=tmp_path / "root",
    )


def test_hook_command_is_not_hijacked_by_module_in_project_dir(tmp_path):
    import subprocess
    import sys

    project = tmp_path / "project"
    fake = project / "senda_argus_hooks" / "cli"
    fake.mkdir(parents=True)
    (project / "senda_argus_hooks" / "__init__.py").write_text("")
    (fake / "__init__.py").write_text("")
    (fake / "main.py").write_text("print('HIJACK')\n")
    command = inst.hook_command("claude_code", "pre", "user", python=sys.executable)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SENDA_ARGUS_")}
    env["SENDA_ARGUS_ENDPOINT"] = "http://127.0.0.1:9"
    env["SENDA_ARGUS_CONFIG"] = str(tmp_path / "none.env")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    stdin = json.dumps(
        {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    )
    result = subprocess.run(
        command,
        shell=True,
        cwd=project,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert "HIJACK" not in result.stdout
    assert result.returncode == 2


def test_failure_events_are_registered_with_the_post_adapter(tmp_path):
    # 失敗と中断で終わった実行は成功のイベントに届かない。Cursor と同じく失敗のイベントも登録する。
    claude = _read(_install("claude_code", "managed", tmp_path))["hooks"]
    assert claude["PostToolUseFailure"][0]["hooks"][0]["command"].endswith(
        "hook claude_code post --config /etc/senda-argus/hooks.env"
    )
    copilot = _read(_install("copilot_cli", "managed", tmp_path))["hooks"]
    assert copilot["postToolUseFailure"][0]["bash"].endswith(
        "hook copilot_cli post --config /etc/senda-argus/hooks.env"
    )
    again = _read(_install("claude_code", "managed", tmp_path))["hooks"]
    assert len(again["PostToolUseFailure"]) == 1


def test_user_scope_keeps_user_site_packages_importable(monkeypatch):
    # -I は利用者の site-packages を外す。pip install --user で入れた物が読めなくなるので、
    # 利用者の階層の導入は作業ディレクトリだけを外す -P を使う。-P は 3.11 以降。
    monkeypatch.setattr(inst.sys, "version_info", (3, 12, 0))
    assert " -P -m " in inst.hook_command("claude_code", "pre", "user", python=PY)
    assert " -I -m " in inst.hook_command("claude_code", "pre", "managed", python=PY)
    monkeypatch.setattr(inst.sys, "version_info", (3, 10, 0))
    assert " -I -m " in inst.hook_command("claude_code", "pre", "user", python=PY)


def test_all_with_an_unsupported_product_writes_nothing(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(
        inst, "install_product", lambda product, *a, **k: written.append(product)
    )
    with pytest.raises(InstallError):
        inst.run_install(list(inst.PRODUCTS), "managed", dry_run=False)
    assert written == []
