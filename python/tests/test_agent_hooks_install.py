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
    monkeypatch.setattr(inst, "managed_dir_check", lambda path: None)


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
    # 書く前に全ての製品で書く物を決める段と、書く段で 2 回ずつ呼ぶ。
    assert calls == [("cursor", "user")] * 2 + [("cursor", "managed")] * 2


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


# 13 製品の導入


@pytest.mark.parametrize(
    ("product", "platform", "rel"),
    [
        ("windsurf", "linux", "root/etc/devin/hooks.json"),
        ("windsurf", "darwin", "root/Library/Application Support/Devin/hooks.json"),
        ("gemini_cli", "linux", "root/etc/gemini-cli/settings.json"),
        (
            "gemini_cli",
            "darwin",
            "root/Library/Application Support/GeminiCli/settings.json",
        ),
        ("augment_code", "linux", "root/etc/augment/settings.json"),
        ("augment_code", "darwin", "root/etc/augment/settings.json"),
    ],
)
def test_new_managed_paths_per_os(tmp_path, product, platform, rel):
    result = _install(product, "managed", tmp_path, platform=platform)
    assert result.path == str(tmp_path / rel)
    assert os.stat(result.path).st_mode & 0o022 == 0


NO_MANAGED = [
    "vscode_copilot",
    "qwen_code",
    "kiro",
    "amazon_q_cli",
    "factory_droid",
    "crush",
    "goose",
    "junie_cli",
    "cline",
    "antigravity_cli",
]
NO_USER = ["vscode_copilot", "qwen_code", "amazon_q_cli"]


@pytest.mark.parametrize("product", NO_MANAGED)
def test_managed_is_refused_without_an_official_location(tmp_path, product):
    with pytest.raises(InstallError):
        _install(product, "managed", tmp_path)
    assert not (tmp_path / "root").exists()
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("product", NO_USER)
def test_user_is_refused_without_an_official_location(tmp_path, product):
    with pytest.raises(InstallError):
        _install(product, "user", tmp_path)
    assert not (tmp_path / "home").exists()


USER_PATHS = {
    "windsurf": "home/.codeium/windsurf/hooks.json",
    "gemini_cli": "home/.gemini/settings.json",
    "kiro": "home/.kiro/hooks/senda-argus.json",
    "factory_droid": "home/.factory/hooks.json",
    "crush": "home/.config/crush/crush.json",
    "goose": "home/.agents/plugins/senda-argus/hooks/hooks.json",
    "augment_code": "home/.augment/settings.json",
    "junie_cli": "home/.junie/config.json",
    "cline": "home/Documents/Cline/Hooks/PreToolUse",
    "antigravity_cli": "home/.gemini/config/hooks.json",
}


@pytest.mark.parametrize("product", list(USER_PATHS))
def test_new_user_paths_and_idempotent(tmp_path, product):
    first = _install(product, "user", tmp_path)
    assert first.path == str(tmp_path / USER_PATHS[product])
    assert first.changed is True
    snapshot = {f: open(f, encoding="utf-8").read() for f in first.files}
    second = _install(product, "user", tmp_path)
    assert second.changed is False
    assert second.backup is None
    # 導入し直しで項目を重ねない。2 回目は 1 回目と同じ中身のまま。
    assert {f: open(f, encoding="utf-8").read() for f in second.files} == snapshot


def _commands(data):
    """設定の中の全ての command を集める。"""
    found = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("command", "bash") and isinstance(item, str):
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found


def test_windsurf_registers_every_tool_hook(tmp_path):
    hooks_ = _read(_install("windsurf", "managed", tmp_path))["hooks"]
    for action in ("run_command", "read_code", "write_code", "mcp_tool_use"):
        assert hooks_[f"pre_{action}"][0]["command"].endswith(
            "hook windsurf pre --config /etc/senda-argus/hooks.env"
        )
        assert hooks_[f"post_{action}"][0]["command"].endswith(
            "hook windsurf post --config /etc/senda-argus/hooks.env"
        )
    assert "pre_user_prompt" not in hooks_


def test_gemini_shape_timeout_in_milliseconds(tmp_path):
    hooks_ = _read(_install("gemini_cli", "managed", tmp_path))["hooks"]
    pre = hooks_["BeforeTool"][0]
    assert pre["matcher"] == ".*"
    assert pre["hooks"][0]["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC * 1000
    assert pre["hooks"][0]["command"].endswith(
        "hook gemini_cli pre --config /etc/senda-argus/hooks.env"
    )
    assert hooks_["AfterTool"][0]["hooks"][0]["command"].endswith(
        "hook gemini_cli post --config /etc/senda-argus/hooks.env"
    )


def test_goose_pre_hook_fails_closed_and_plugin_manifest_is_written(tmp_path):
    result = _install("goose", "user", tmp_path)
    hooks_ = _read(result)["hooks"]
    pre = hooks_["PreToolUse"][0]
    # "*" 単独の matcher は無効で、規則ごと飛ばされる。
    assert "matcher" not in pre
    assert pre["hooks"][0]["on_failure"] == "block"
    assert pre["hooks"][0]["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    assert "on_failure" not in hooks_["PostToolUse"][0]["hooks"][0]
    manifest = tmp_path / "home/.agents/plugins/senda-argus/plugin.json"
    assert json.loads(manifest.read_text())["name"] == "senda-argus"
    assert str(manifest) in result.files


def test_goose_existing_plugin_manifest_is_kept(tmp_path):
    manifest = tmp_path / "home/.agents/plugins/senda-argus/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name": "senda-argus", "version": "9"}')
    _install("goose", "user", tmp_path)
    assert manifest.read_text() == '{"name": "senda-argus", "version": "9"}'


def test_augment_uses_wrapper_scripts_and_user_context(tmp_path):
    result = _install("augment_code", "managed", tmp_path)
    pre = _read(result)["hooks"]["PreToolUse"][0]["hooks"][0]
    script = tmp_path / "root/etc/augment/hooks/senda-argus-hook-pre.sh"
    # command は拡張子のある script の場所だけを受ける。
    assert pre["command"] == str(script)
    assert pre["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC * 1000
    assert pre["metadata"] == {"includeUserContext": True}
    text = script.read_text()
    assert text.startswith("#!/bin/sh\nexec ")
    assert "hook augment_code pre --config /etc/senda-argus/hooks.env" in text
    assert os.stat(script).st_mode & 0o777 == 0o755
    post = tmp_path / "root/etc/augment/hooks/senda-argus-hook-post.sh"
    assert "hook augment_code post" in post.read_text()
    assert str(script) in result.files


def test_augment_reinstall_replaces_its_wrapper_entry(tmp_path):
    path = tmp_path / "home/.augment/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "model": "x",
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/opt/old/senda-argus-hook-pre.sh",
                                },
                                {"type": "command", "command": "/opt/dlp.sh"},
                            ]
                        }
                    ]
                },
            }
        )
    )
    data = _read(_install("augment_code", "user", tmp_path))
    assert data["model"] == "x"
    commands = _commands(data["hooks"]["PreToolUse"])
    assert "/opt/dlp.sh" in commands
    assert "/opt/old/senda-argus-hook-pre.sh" not in commands
    assert sum("senda-argus-hook-pre.sh" in c for c in commands) == 1


def test_cline_writes_executable_scripts(tmp_path):
    result = _install("cline", "user", tmp_path)
    pre = tmp_path / "home/Documents/Cline/Hooks/PreToolUse"
    post = tmp_path / "home/Documents/Cline/Hooks/PostToolUse"
    assert "hook cline pre" in pre.read_text()
    assert "hook cline post" in post.read_text()
    assert os.stat(pre).st_mode & 0o100
    assert result.files == [str(pre), str(post)]


def test_cline_does_not_overwrite_a_foreign_hook(tmp_path):
    pre = tmp_path / "home/Documents/Cline/Hooks/PreToolUse"
    pre.parent.mkdir(parents=True)
    pre.write_text("#!/bin/sh\necho '{\"cancel\": false}'\n")
    with pytest.raises(InstallError, match="Argus でない"):
        _install("cline", "user", tmp_path)
    assert pre.read_text() == "#!/bin/sh\necho '{\"cancel\": false}'\n"
    # 書き込みの前に失敗し、もう片方のファイルも書かない。
    assert not (tmp_path / "home/Documents/Cline/Hooks/PostToolUse").exists()


def test_kiro_hook_file_shape_and_keeps_other_hooks(tmp_path):
    path = tmp_path / "home/.kiro/hooks/senda-argus.json"
    path.parent.mkdir(parents=True)
    other = {
        "name": "lint",
        "trigger": "PostFileSave",
        "action": {"type": "command", "command": "npx eslint --fix"},
    }
    old = {
        "name": "senda-argus-pre",
        "trigger": "PreToolUse",
        "action": {
            "type": "command",
            "command": "/old/py -m senda_argus_hooks.cli.main hook kiro pre",
        },
    }
    path.write_text(json.dumps({"version": "v1", "hooks": [other, old]}))
    data = _read(_install("kiro", "user", tmp_path))
    assert data["version"] == "v1"
    assert data["hooks"][0] == other
    ours = [h for h in data["hooks"] if h["name"].startswith("senda-argus")]
    assert [h["trigger"] for h in ours] == ["PreToolUse", "PostToolUse"]
    assert ours[0]["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    assert "matcher" not in ours[0]
    assert "/old/py" not in json.dumps(data)


def test_factory_events_are_at_the_top_level(tmp_path):
    path = tmp_path / "home/.factory/hooks.json"
    path.parent.mkdir(parents=True)
    other = {
        "matcher": "Execute",
        "hooks": [{"type": "command", "command": "/usr/local/bin/audit.sh"}],
    }
    path.write_text(json.dumps({"PreToolUse": [other]}))
    data = _read(_install("factory_droid", "user", tmp_path))
    assert "hooks" not in data
    assert data["PreToolUse"][0] == other
    assert data["PreToolUse"][1]["hooks"][0]["command"].endswith(
        "hook factory_droid pre"
    )
    assert data["PostToolUse"][0]["hooks"][0]["command"].endswith(
        "hook factory_droid post"
    )


def test_crush_and_junie_have_only_a_pre_hook(tmp_path):
    crush = _read(_install("crush", "user", tmp_path))["hooks"]
    assert list(crush) == ["PreToolUse"]
    assert crush["PreToolUse"][0]["matcher"] == ".*"
    assert crush["PreToolUse"][0]["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    junie = _read(_install("junie_cli", "user", tmp_path))["hooks"]
    assert list(junie) == ["PreToolUse"]
    assert (
        junie["PreToolUse"][0]["hooks"][0]["timeout"] == inst.PRODUCT_HOOK_TIMEOUT_SEC
    )


def test_antigravity_named_entry_keeps_other_named_hooks(tmp_path):
    path = tmp_path / "home/.gemini/config/hooks.json"
    path.parent.mkdir(parents=True)
    other = {
        "PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./lint.sh"}]}]
    }
    path.write_text(json.dumps({"my-linter-hook": other}))
    result = _install("antigravity_cli", "user", tmp_path)
    data = _read(result)
    assert data["my-linter-hook"] == other
    ours = data["senda-argus"]
    assert ours["PreToolUse"][0]["matcher"] == ".*"
    assert ours["PreToolUse"][0]["hooks"][0]["command"].endswith(
        "hook antigravity_cli pre"
    )
    assert result.backup is not None
    assert json.loads(open(result.backup, encoding="utf-8").read()) == {
        "my-linter-hook": other
    }


@pytest.mark.parametrize(
    ("product", "existing"),
    [
        (
            "windsurf",
            {"hooks": {"pre_run_command": [{"command": "/opt/guard.sh"}]}, "x": 1},
        ),
        (
            "gemini_cli",
            {
                "theme": "dark",
                "hooks": {
                    "BeforeTool": [
                        {"hooks": [{"type": "command", "command": "/opt/g.sh"}]}
                    ]
                },
            },
        ),
        (
            "crush",
            {
                "options": {"debug": True},
                "hooks": {"PreToolUse": [{"command": "/opt/c.sh"}]},
            },
        ),
        (
            "junie_cli",
            {
                "model": "m",
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "/opt/j.sh"}]}
                    ]
                },
            },
        ),
        (
            "goose",
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "/opt/o.sh"}]}
                    ]
                }
            },
        ),
    ],
)
def test_existing_settings_are_kept(tmp_path, product, existing):
    path = tmp_path / USER_PATHS[product]
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(existing))
    result = _install(product, "user", tmp_path)
    data = _read(result)
    for key, value in existing.items():
        if key != "hooks":
            assert data[key] == value
    for event, entries in existing["hooks"].items():
        assert data["hooks"][event][0] == entries[0]
    assert sum(HOOK_MARKER in c for c in _commands(data)) >= 1
    assert json.loads(open(result.backup, encoding="utf-8").read()) == existing


def test_all_user_scope_writes_nothing_when_a_product_has_no_location(monkeypatch):
    # Qwen Code と Amazon Q と VS Code は利用者の置き場所を持たない。all は書く前に失敗する。
    written: list[str] = []
    monkeypatch.setattr(
        inst, "install_product", lambda product, *a, **k: written.append(product)
    )
    with pytest.raises(InstallError):
        inst.run_install(list(inst.PRODUCTS), "user", dry_run=False)
    assert written == []


def test_cli_offers_every_product(capsys):
    from senda_argus_hooks.cli.main import build_parser

    parser = build_parser()
    for product in [*inst.PRODUCTS, "all"]:
        args = parser.parse_args(["hooks-install", "--product", product, "--dry-run"])
        assert args.product == product
    assert len(inst.PRODUCTS) == 17


def test_gemini_managed_prints_the_override_notice(monkeypatch):
    import io

    monkeypatch.setattr(
        inst, "install_product", lambda product, scope, dry_run=False: None
    )
    stream = io.StringIO()
    inst.run_install(["gemini_cli"], "managed", stream=stream)
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" in stream.getvalue()
    stream = io.StringIO()
    inst.run_install(["cursor"], "managed", stream=stream)
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" not in stream.getvalue()


@pytest.mark.parametrize(
    ("rel", "content"),
    [
        (".factory/settings.json", {"model": "m", "hooks": {"PreToolUse": []}}),
        (".factory/hooks/hooks.json", {"PreToolUse": []}),
    ],
)
def test_factory_refuses_to_shadow_hooks_read_only_without_hooks_json(
    tmp_path, rel, content
):
    other = tmp_path / "home" / rel
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps(content))
    with pytest.raises(InstallError, match="読まなくなる"):
        _install("factory_droid", "user", tmp_path)
    assert not (tmp_path / "home/.factory/hooks.json").exists()


def test_factory_settings_without_hooks_does_not_block(tmp_path):
    other = tmp_path / "home/.factory/settings.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({"model": "m"}))
    result = _install("factory_droid", "user", tmp_path)
    assert result.changed is True


def test_factory_existing_hooks_json_is_merged_even_with_settings_hooks(tmp_path):
    # hooks.json が在れば製品は settings.json を読まないため、今の読まれ方は変わらない。
    (tmp_path / "home/.factory").mkdir(parents=True)
    (tmp_path / "home/.factory/settings.json").write_text('{"hooks": {}}')
    (tmp_path / "home/.factory/hooks.json").write_text("{}")
    assert _install("factory_droid", "user", tmp_path).changed is True


@pytest.mark.parametrize(
    ("platform", "rel"),
    [
        ("linux", "etc/windsurf/hooks.json"),
        ("darwin", "Library/Application Support/Windsurf/hooks.json"),
    ],
)
def test_windsurf_managed_refuses_to_shadow_the_legacy_system_file(
    tmp_path, platform, rel
):
    legacy = tmp_path / "root" / rel
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"hooks": {}}')
    with pytest.raises(InstallError, match="読まなくなる"):
        _install("windsurf", "managed", tmp_path, platform=platform)
    assert not (tmp_path / "root/etc/devin/hooks.json").exists()


def test_augment_managed_refuses_a_wrapper_dir_writable_by_others(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(inst, "managed_dir_check", inst._check_root_owned)
    hooks_dir = tmp_path / "root/etc/augment/hooks"
    hooks_dir.mkdir(parents=True)
    # テストの利用者の所有で、root の所有でない。
    with pytest.raises(InstallError, match="root の所有でない"):
        _install("augment_code", "managed", tmp_path)
    assert not (tmp_path / "root/etc/augment/settings.json").exists()
    assert list(hooks_dir.iterdir()) == []


def test_run_install_plans_every_product_before_writing(tmp_path, monkeypatch):
    # 後の製品で他者の hook と衝突するときは、先の製品も書かない。
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    foreign = tmp_path / "home/Documents/Cline/Hooks/PreToolUse"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("#!/bin/sh\necho '{\"cancel\": false}'\n")
    with pytest.raises(InstallError, match="Argus でない"):
        inst.run_install(["kiro", "cline"], "user")
    assert not (tmp_path / "home/.kiro").exists()


def test_supported_products_lists_what_the_scope_can_install_and_why_the_rest_cannot():
    supported, skipped = inst.supported_products("user")
    assert set(supported) | set(skipped) == set(inst.PRODUCTS)
    assert not set(supported) & set(skipped)
    for product in ("qwen_code", "amazon_q_cli", "vscode_copilot"):
        assert product in skipped and skipped[product]
    for product in ("claude_code", "cursor", "kiro", "goose"):
        assert product in supported


def test_all_installs_only_the_products_the_scope_supports(monkeypatch, capsys):
    from senda_argus_hooks.cli import main as cli_main

    written: list[list[str]] = []
    monkeypatch.setattr(
        inst,
        "run_install",
        lambda products, scope, **k: written.append(list(products)) or [],
    )
    args = cli_main.build_parser().parse_args(
        ["hooks-install", "--product", "all", "--scope", "user", "--dry-run"]
    )
    assert cli_main.cmd_hooks_install(args) == 0
    supported, skipped = inst.supported_products("user")
    assert written == [supported]
    err = capsys.readouterr().err
    for product in skipped:
        assert f"飛ばした製品 {product}" in err


def test_a_named_product_without_a_location_still_fails_before_writing(monkeypatch):
    written: list[str] = []
    monkeypatch.setattr(
        inst, "install_product", lambda product, *a, **k: written.append(product)
    )
    with pytest.raises(InstallError):
        inst.run_install(["claude_code", "qwen_code"], "user", dry_run=False)
    assert written == []


def test_a_foreign_antigravity_hook_under_our_name_is_not_overwritten(tmp_path):
    path = tmp_path / "home" / ".gemini" / "config" / "hooks.json"
    path.parent.mkdir(parents=True)
    foreign = {"senda-argus": {"PreToolUse": [{"command": "/opt/other/check.sh"}]}}
    path.write_text(json.dumps(foreign), encoding="utf-8")
    with pytest.raises(InstallError):
        _install("antigravity_cli", "user", tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == foreign


def test_our_own_antigravity_hook_is_replaced_on_reinstall(tmp_path):
    _install("antigravity_cli", "user", tmp_path)
    again = _install("antigravity_cli", "user", tmp_path)
    data = json.loads(open(again.path, encoding="utf-8").read())
    assert list(data) == ["senda-argus"]


def test_cline_install_reports_the_manual_enable_step(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(inst.Path, "home", lambda: tmp_path / "home")
    results = inst.run_install(["cline"], "user", dry_run=True)
    assert "Enable Hooks" in results[0].notice
    assert "Enable Hooks" in capsys.readouterr().err
    other = inst.run_install(["kiro"], "user", dry_run=True)
    assert other[0].notice == ""
