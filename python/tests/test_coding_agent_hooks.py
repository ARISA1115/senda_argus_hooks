from __future__ import annotations

import io
import json
import os
import stat

import pytest

from senda_argus_hooks.integrations import coding_agent_hooks as hooks
from senda_argus_hooks.integrations.coding_agent_hooks import (
    HookConfig,
    adapter_for,
    decide,
    run_post,
    run_pre,
)


class FakeServer:
    """約束の POST /v1/hooks/precheck と postcall の偽の応答。応答の並びを順に返す。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, str, float]] = []

    def __call__(self, url, body, api_key, timeout):
        self.calls.append((url, json.loads(json.dumps(body)), api_key, timeout))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _config(**overrides):
    base = {"endpoint": "http://argus.test", "api_key": "k", "agent_id": "agent-1"}
    base.update(overrides)
    return HookConfig(**base)


def _ok(decision, **extra):
    body = {"decision": decision, "request_id": None, "reason": "", "wait_hint_sec": 0}
    body.update(extra)
    return (200, body)


@pytest.fixture
def trial_env(monkeypatch, tmp_path):
    """試用の導入の設定。環境変数から読む。設定ファイルは空の場所へ向ける。"""
    monkeypatch.setattr("senda_argus_hooks.config._LOADED", True)
    monkeypatch.setenv("SENDA_ARGUS_ENDPOINT", "http://argus.test")
    monkeypatch.setenv("SENDA_ARGUS_API_KEY", "k")
    monkeypatch.setenv("SENDA_ARGUS_AGENT_ID", "agent-1")
    monkeypatch.delenv("SENDA_ARGUS_HOOK_FAIL_OPEN", raising=False)
    monkeypatch.delenv("SENDA_ARGUS_HOOK_WAIT_LIMIT_SEC", raising=False)
    monkeypatch.delenv("SENDA_ARGUS_HOOK_REQUEST_TIMEOUT_SEC", raising=False)
    return monkeypatch


def _stdin(data):
    return io.StringIO(json.dumps(data))


# 製品ごとの実物の形の入力。
CLAUDE_PRE = {
    "session_id": "s-1",
    "transcript_path": "/tmp/t.jsonl",
    "cwd": "/repo",
    "permission_mode": "default",
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf build", "description": "片付け"},
    "tool_use_id": "toolu_01",
}
CODEX_PRE = {
    "session_id": "cx-1",
    "transcript_path": None,
    "cwd": "/repo",
    "hook_event_name": "PreToolUse",
    "model": "gpt-5",
    "turn_id": "t-1",
    "tool_name": "apply_patch",
    "tool_use_id": "call_9",
    "tool_input": {"command": "*** Begin Patch"},
}
COPILOT_PRE = {
    "sessionId": "cp-1",
    "timestamp": 1760000000,
    "cwd": "/repo",
    "toolName": "bash",
    "toolArgs": json.dumps({"command": "curl https://example.com"}),
}
CURSOR_PRE = {
    "conversation_id": "conv-1",
    "generation_id": "g-1",
    "model": "m",
    "hook_event_name": "preToolUse",
    "cursor_version": "1.7.2",
    "workspace_roots": ["/repo"],
    "user_email": "dev@example.com",
    "transcript_path": None,
    "tool_name": "Shell",
    "tool_input": {"command": "npm install", "working_directory": "/repo"},
    "tool_use_id": "abc123",
    "cwd": "/repo",
    "agent_message": "Installing",
}


@pytest.mark.parametrize(
    ("product", "data", "expected"),
    [
        (
            "claude_code",
            CLAUDE_PRE,
            {
                "product": "claude_code",
                "agent_id": "agent-1",
                "session_id": "s-1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf build", "description": "片付け"},
                "tool_use_id": "toolu_01",
                "cwd": "/repo",
                "permission_mode": "default",
            },
        ),
        (
            "codex",
            CODEX_PRE,
            {
                "product": "codex",
                "agent_id": "agent-1",
                "session_id": "cx-1",
                "tool_name": "apply_patch",
                "tool_input": {"command": "*** Begin Patch"},
                "tool_use_id": "call_9",
                "cwd": "/repo",
            },
        ),
        (
            "copilot_cli",
            COPILOT_PRE,
            {
                "product": "copilot_cli",
                "agent_id": "agent-1",
                "session_id": "cp-1",
                "tool_name": "bash",
                "tool_input": {"command": "curl https://example.com"},
                "cwd": "/repo",
            },
        ),
        (
            "cursor",
            CURSOR_PRE,
            {
                "product": "cursor",
                "agent_id": "agent-1",
                "session_id": "conv-1",
                "tool_name": "Shell",
                "tool_input": {"command": "npm install", "working_directory": "/repo"},
                "tool_use_id": "abc123",
                "cwd": "/repo",
                "user_email": "dev@example.com",
            },
        ),
    ],
)
def test_pre_sends_contract_body_and_allow_maps_to_product_output(
    trial_env, product, data, expected
):
    server = FakeServer([_ok("allow")])
    trial_env.setattr(hooks, "post_json", server)
    out = run_pre(adapter_for(product), _stdin(data), None)
    assert server.calls[0][0] == "http://argus.test/v1/hooks/precheck"
    assert server.calls[0][1] == expected
    assert server.calls[0][2] == "k"
    assert out.exit_code == 0
    if product == "cursor":
        assert json.loads(out.stdout) == {"permission": "allow"}
    else:
        # 許可は何も書かず、製品の利用者の許可の確認を飛ばさない。
        assert out.stdout == ""


def test_copilot_snake_case_input_is_read(trial_env):
    server = FakeServer([_ok("allow")])
    trial_env.setattr(hooks, "post_json", server)
    data = {
        "hook_event_name": "PreToolUse",
        "session_id": "cp-2",
        "timestamp": "2026-10-16T00:00:00Z",
        "cwd": "/repo",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    out = run_pre(adapter_for("copilot_cli"), _stdin(data), None)
    assert out.exit_code == 0
    assert server.calls[0][1]["session_id"] == "cp-2"
    assert server.calls[0][1]["tool_input"] == {"command": "ls"}


EXPECTED_DENY = {
    "claude_code": lambda r: {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": r,
        }
    },
    "codex": lambda r: {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": r,
        }
    },
    "copilot_cli": lambda r: {
        "permissionDecision": "deny",
        "permissionDecisionReason": r,
    },
    "cursor": lambda r: {"permission": "deny", "user_message": r, "agent_message": r},
}
PRE_INPUTS = {
    "claude_code": CLAUDE_PRE,
    "codex": CODEX_PRE,
    "copilot_cli": COPILOT_PRE,
    "cursor": CURSOR_PRE,
}


@pytest.mark.parametrize("product", ["claude_code", "codex", "copilot_cli", "cursor"])
def test_deny_returns_product_json_and_exit_code_2(trial_env, product):
    trial_env.setattr(
        hooks, "post_json", FakeServer([_ok("deny", reason="段階が止める")])
    )
    out = run_pre(adapter_for(product), _stdin(PRE_INPUTS[product]), None)
    assert out.exit_code == 2
    assert json.loads(out.stdout) == EXPECTED_DENY[product]("段階が止める")
    assert out.stderr == "段階が止める"


@pytest.mark.parametrize("product", ["claude_code", "codex", "copilot_cli", "cursor"])
def test_pending_without_wait_returns_deny_with_request_id(trial_env, product):
    server = FakeServer(
        [_ok("pending", request_id="req-42", reason="管理者の承認を待つ")]
    )
    trial_env.setattr(hooks, "post_json", server)
    out = run_pre(adapter_for(product), _stdin(PRE_INPUTS[product]), None)
    assert out.exit_code == 2
    assert "req-42" in out.stderr
    assert len(server.calls) == 1


def test_pending_wait_resends_every_second_until_allow():
    clock = FakeClock()
    server = FakeServer(
        [
            _ok("pending", request_id="req-1", wait_hint_sec=10),
            _ok("pending", request_id="req-1", wait_hint_sec=10),
            _ok("allow"),
        ]
    )
    call = adapter_for("claude_code").parse_pre(CLAUDE_PRE)
    original = hooks.post_json
    hooks.post_json = server
    try:
        verdict = decide(
            call, _config(wait_limit_sec=20), clock=clock, sleep=clock.sleep
        )
    finally:
        hooks.post_json = original
    assert verdict.allow is True
    assert len(server.calls) == 3
    # 再送は同じ要求で、間隔は 1 秒。
    assert server.calls[0][1] == server.calls[2][1]
    assert clock.now == 2.0


def test_pending_wait_stops_at_wait_hint_and_denies_with_request_id():
    clock = FakeClock()
    server = FakeServer([_ok("pending", request_id="req-7", wait_hint_sec=3)])
    call = adapter_for("claude_code").parse_pre(CLAUDE_PRE)
    original = hooks.post_json
    hooks.post_json = server
    try:
        verdict = decide(
            call, _config(wait_limit_sec=20), clock=clock, sleep=clock.sleep
        )
    finally:
        hooks.post_json = original
    assert verdict.allow is False
    assert "req-7" in verdict.reason
    # 0 秒の初回と、1、2、3 秒の再送で 4 回。wait_hint の 3 秒を超えて送らない。
    assert len(server.calls) == 4
    assert clock.now == 3.0


def test_internal_limit_denies_before_wait_hint_ends():
    clock = FakeClock()
    server = FakeServer([_ok("pending", request_id="req-8", wait_hint_sec=600)])
    call = adapter_for("claude_code").parse_pre(CLAUDE_PRE)
    original = hooks.post_json
    hooks.post_json = server
    try:
        verdict = decide(
            call, _config(wait_limit_sec=5), clock=clock, sleep=clock.sleep
        )
    finally:
        hooks.post_json = original
    assert verdict.allow is False
    assert clock.now <= 5.0
    assert "内部の上限" in verdict.reason
    assert "req-8" in verdict.reason


def test_slow_first_response_past_internal_limit_denies_even_if_allow():
    clock = FakeClock()

    def slow(url, body, api_key, timeout):
        clock.now += 30
        return _ok("allow")

    call = adapter_for("claude_code").parse_pre(CLAUDE_PRE)
    original = hooks.post_json
    hooks.post_json = slow
    try:
        verdict = decide(
            call, _config(wait_limit_sec=20), clock=clock, sleep=clock.sleep
        )
    finally:
        hooks.post_json = original
    assert verdict.allow is False
    assert "内部の上限" in verdict.reason


def test_request_timeout_is_bounded_by_internal_limit(trial_env):
    trial_env.setenv("SENDA_ARGUS_HOOK_WAIT_LIMIT_SEC", "999")
    trial_env.setenv("SENDA_ARGUS_HOOK_REQUEST_TIMEOUT_SEC", "999")
    config = hooks.load_hook_config("copilot_cli", None)
    # Copilot CLI の 30 秒より短い。
    assert config.wait_limit_sec < 30
    assert config.request_timeout_sec <= config.wait_limit_sec
    server = FakeServer([_ok("allow")])
    trial_env.setattr(hooks, "post_json", server)
    decide(adapter_for("copilot_cli").parse_pre(COPILOT_PRE), config)
    assert server.calls[0][3] <= config.wait_limit_sec


@pytest.mark.parametrize(
    "failure",
    [
        hooks.TransportError("URLError"),
        (503, None),
        (500, None),
        (200, {"decision": "maybe"}),
        (200, ["x"]),
    ],
)
def test_unreachable_or_bad_response_denies_by_default(trial_env, failure):
    trial_env.setattr(hooks, "post_json", FakeServer([failure]))
    out = run_pre(adapter_for("claude_code"), _stdin(CLAUDE_PRE), None)
    assert out.exit_code == 2


@pytest.mark.parametrize(
    "failure",
    [hooks.TransportError("URLError"), (503, None), (200, {"decision": "maybe"})],
)
def test_fail_open_allows_only_when_set(trial_env, failure):
    trial_env.setenv("SENDA_ARGUS_HOOK_FAIL_OPEN", "true")
    trial_env.setattr(hooks, "post_json", FakeServer([failure]))
    out = run_pre(adapter_for("claude_code"), _stdin(CLAUDE_PRE), None)
    assert out.exit_code == 0


# 413 は本文が大きすぎ、429 は呼び出しが多すぎる拒否。fail_open でも素通りにしない。
@pytest.mark.parametrize("status", [400, 401, 403, 413, 429])
def test_rejected_request_denies_even_with_fail_open(trial_env, status):
    trial_env.setenv("SENDA_ARGUS_HOOK_FAIL_OPEN", "true")
    trial_env.setattr(hooks, "post_json", FakeServer([(status, None)]))
    out = run_pre(adapter_for("claude_code"), _stdin(CLAUDE_PRE), None)
    assert out.exit_code == 2


def test_real_http_connection_refused_denies(trial_env):
    # 実物の urllib の経路で、接続できない送信先は既定で遮断になる。
    trial_env.setenv("SENDA_ARGUS_ENDPOINT", "http://127.0.0.1:9")
    out = run_pre(adapter_for("claude_code"), _stdin(CLAUDE_PRE), None)
    assert out.exit_code == 2


def test_failure_during_wait_does_not_turn_into_fail_open_allow():
    clock = FakeClock()
    server = FakeServer(
        [
            _ok("pending", request_id="req-9", wait_hint_sec=10),
            hooks.TransportError("x"),
        ]
    )
    call = adapter_for("claude_code").parse_pre(CLAUDE_PRE)
    original = hooks.post_json
    hooks.post_json = server
    try:
        verdict = decide(call, _config(fail_open=True), clock=clock, sleep=clock.sleep)
    finally:
        hooks.post_json = original
    assert verdict.allow is False
    assert "req-9" in verdict.reason


def test_bad_product_input_denies(trial_env):
    trial_env.setattr(hooks, "post_json", FakeServer([_ok("allow")]))
    out = run_pre(adapter_for("claude_code"), io.StringIO("not json"), None)
    assert out.exit_code == 2
    bad_args = dict(COPILOT_PRE, toolArgs="not json")
    out = run_pre(adapter_for("copilot_cli"), _stdin(bad_args), None)
    assert out.exit_code == 2


def test_missing_endpoint_denies(trial_env):
    trial_env.delenv("SENDA_ARGUS_ENDPOINT")
    out = run_pre(adapter_for("cursor"), _stdin(CURSOR_PRE), None)
    assert out.exit_code == 2
    assert json.loads(out.stdout)["permission"] == "deny"


# 管理の設定ファイル


def _write_managed(tmp_path, text, mode=0o644):
    path = tmp_path / "hooks.env"
    path.write_text(text)
    os.chmod(path, mode)
    return path


def test_managed_config_ignores_env_and_reads_only_file(trial_env, tmp_path):
    path = _write_managed(
        tmp_path, "SENDA_ARGUS_ENDPOINT=http://managed.test\nSENDA_ARGUS_API_KEY=mk\n"
    )
    trial_env.setattr(hooks, "managed_owner_check", lambda st: None)
    # 利用者が環境変数で fail_open と送信先を変えても、管理の導入では効かない。
    trial_env.setenv("SENDA_ARGUS_HOOK_FAIL_OPEN", "true")
    trial_env.setenv("SENDA_ARGUS_ENDPOINT", "http://attacker.test")
    config = hooks.load_hook_config("claude_code", str(path))
    assert config.endpoint == "http://managed.test"
    assert config.api_key == "mk"
    assert config.fail_open is False


def test_managed_config_not_owned_by_root_denies(trial_env, tmp_path):
    path = _write_managed(
        tmp_path,
        "SENDA_ARGUS_ENDPOINT=http://managed.test\nSENDA_ARGUS_HOOK_FAIL_OPEN=true\n",
    )
    trial_env.setattr(hooks, "post_json", FakeServer([hooks.TransportError("x")]))
    # 試験は root でない利用者で動くため、実物の所有者の検査で落ちる。
    assert os.stat(path).st_uid != 0
    out = run_pre(adapter_for("claude_code"), _stdin(CLAUDE_PRE), str(path))
    assert out.exit_code == 2
    assert "root" in out.stderr


def test_managed_owner_check_rejects_group_writable():
    fake = os.stat_result((stat.S_IFREG | 0o664, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    with pytest.raises(hooks.HookConfigError):
        hooks._check_managed_owner(fake)
    ok = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    hooks._check_managed_owner(ok)


# 実行の後の収集器

CLAUDE_POST = dict(
    CLAUDE_PRE,
    hook_event_name="PostToolUse",
    tool_response={"stdout": "x" * 10000, "exit_code": 0},
)
CODEX_POST = dict(CODEX_PRE, hook_event_name="PostToolUse", tool_response="done")
COPILOT_POST = dict(
    COPILOT_PRE, toolResult={"resultType": "success", "textResultForLlm": "ok"}
)
CURSOR_POST = dict(
    CURSOR_PRE, hook_event_name="postToolUse", tool_output='{"exitCode":0}', duration=10
)
CURSOR_FAIL = dict(
    CURSOR_PRE,
    hook_event_name="postToolUseFailure",
    error_message="timed out",
    failure_type="timeout",
)


@pytest.mark.parametrize(
    ("product", "data", "expected_response"),
    [
        ("claude_code", CLAUDE_POST, None),
        ("codex", CODEX_POST, "done"),
        (
            "copilot_cli",
            COPILOT_POST,
            {"resultType": "success", "textResultForLlm": "ok"},
        ),
        ("cursor", CURSOR_POST, '{"exitCode":0}'),
        ("cursor", CURSOR_FAIL, {"error": "timed out", "failure_type": "timeout"}),
    ],
)
def test_post_sends_postcall_with_truncated_response(
    trial_env, product, data, expected_response
):
    server = FakeServer([(200, {"correlation": "matched", "unmatched_total": 0})])
    trial_env.setattr(hooks, "post_json", server)
    out = run_post(adapter_for(product), _stdin(data), None)
    assert out.exit_code == 0
    assert out.stdout == ""
    url, body, _, _ = server.calls[0]
    assert url == "http://argus.test/v1/hooks/postcall"
    assert body["product"] == product
    if expected_response is None:
        assert isinstance(body["tool_response"], str)
        assert len(body["tool_response"]) == hooks.TOOL_RESPONSE_MAX_CHARS
    else:
        assert body["tool_response"] == expected_response


def test_truncate_long_string_to_4096():
    assert len(hooks.truncate_tool_response("あ" * 5000)) == 4096
    assert hooks.truncate_tool_response({"a": 1}) == {"a": 1}


@pytest.mark.parametrize(
    "failure",
    [hooks.TransportError("URLError"), (503, None), RuntimeError("boom")],
)
def test_post_failure_never_blocks(trial_env, failure):
    trial_env.setattr(hooks, "post_json", FakeServer([failure]))
    out = run_post(adapter_for("claude_code"), _stdin(CLAUDE_POST), None)
    assert out.exit_code == 0
    assert out.stdout == ""


def test_post_bad_input_or_missing_config_never_blocks(trial_env):
    trial_env.delenv("SENDA_ARGUS_ENDPOINT")
    out = run_post(adapter_for("cursor"), _stdin(CURSOR_POST), None)
    assert out.exit_code == 0
    out = run_post(adapter_for("cursor"), io.StringIO("{"), None)
    assert out.exit_code == 0


def test_cli_hook_entry_writes_output_and_exit_code(trial_env, monkeypatch, capsys):
    from senda_argus_hooks.cli.main import main

    trial_env.setattr(hooks, "post_json", FakeServer([_ok("deny", reason="止める")]))
    monkeypatch.setattr("sys.stdin", _stdin(CLAUDE_PRE))
    code = main(["hook", "claude_code", "pre"])
    captured = capsys.readouterr()
    assert code == 2
    assert (
        json.loads(captured.out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert "止める" in captured.err


def test_send_postcall_itself_swallows_failures(monkeypatch):
    # 収集器を run_post の外から呼んでも、送れない失敗で例外を出さない。
    call = adapter_for("codex").parse_post(CODEX_POST)
    monkeypatch.setattr(hooks, "post_json", FakeServer([RuntimeError("boom")]))
    assert hooks.send_postcall(call, _config()) is False
    monkeypatch.setattr(hooks, "post_json", FakeServer([(503, None)]))
    assert hooks.send_postcall(call, _config()) is False


def test_slow_server_is_cut_at_internal_limit_in_real_time(trial_env):
    # 応答が届かない間も、全体を内部の上限で打ち切って遮断を返す。
    import time as _time

    def hang(url, body, api_key, timeout):
        _time.sleep(5)
        return _ok("allow")

    trial_env.setattr(hooks, "post_json", hang)
    call = adapter_for("claude_code").parse_pre(CLAUDE_PRE)
    started = _time.monotonic()
    verdict = decide(call, _config(wait_limit_sec=0.5, request_timeout_sec=0.5))
    assert _time.monotonic() - started < 2.0
    assert verdict.allow is False


def test_an_oversized_body_is_refused_before_it_is_sent():
    body = {"tool_input": {"command": "x" * (hooks.REQUEST_BODY_MAX_BYTES + 1)}}
    with pytest.raises(hooks.RejectedError):
        hooks._post_json("http://127.0.0.1:1/x", body, "k", 1.0)


def test_postcall_is_retried_once_on_a_server_error(monkeypatch):
    call = adapter_for("codex").parse_post(CODEX_POST)
    monkeypatch.setattr(hooks, "POSTCALL_RETRY_DELAY_SEC", 0.0)
    server = FakeServer([(503, None), (200, {})])
    monkeypatch.setattr(hooks, "post_json", server)
    assert hooks.send_postcall(call, _config()) is True
    assert len(server.calls) == 2


def test_postcall_is_not_retried_on_a_client_error(monkeypatch):
    call = adapter_for("codex").parse_post(CODEX_POST)
    monkeypatch.setattr(hooks, "POSTCALL_RETRY_DELAY_SEC", 0.0)
    server = FakeServer([(400, None), (200, {})])
    monkeypatch.setattr(hooks, "post_json", server)
    assert hooks.send_postcall(call, _config()) is False
    assert len(server.calls) == 1


def test_claude_failure_event_is_recorded_with_its_error():
    data = dict(CLAUDE_POST, hook_event_name="PostToolUseFailure")
    data.pop("tool_response", None)
    data.update(error="Command exited with non-zero status code 1", is_interrupt=False)
    call = adapter_for("claude_code").parse_post(data)
    assert call.has_response is True
    assert call.tool_response == {
        "error": "Command exited with non-zero status code 1",
        "is_interrupt": False,
    }
