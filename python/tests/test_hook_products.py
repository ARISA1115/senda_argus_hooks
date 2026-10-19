"""13 製品の hook のアダプタのテスト。入力は各製品の公式の文書の例と同じ形で書く。"""

from __future__ import annotations

import io
import json
import os
import time

import pytest

from senda_argus_hooks.integrations import coding_agent_hooks as hooks
from senda_argus_hooks.integrations.coding_agent_hooks import (
    adapter_for,
    run_post,
    run_pre,
)


class FakeServer:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, body, api_key, timeout):
        self.calls.append((url, json.loads(json.dumps(body))))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _ok(decision, **extra):
    body = {"decision": decision, "request_id": None, "reason": "", "wait_hint_sec": 0}
    body.update(extra)
    return (200, body)


@pytest.fixture
def trial_env(monkeypatch):
    monkeypatch.setattr("senda_argus_hooks.config._LOADED", True)
    monkeypatch.setenv("SENDA_ARGUS_ENDPOINT", "http://argus.test")
    monkeypatch.setenv("SENDA_ARGUS_API_KEY", "k")
    monkeypatch.setenv("SENDA_ARGUS_AGENT_ID", "agent-1")
    for key in (
        "SENDA_ARGUS_HOOK_FAIL_OPEN",
        "SENDA_ARGUS_HOOK_WAIT_LIMIT_SEC",
        "SENDA_ARGUS_HOOK_REQUEST_TIMEOUT_SEC",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _stdin(data):
    return io.StringIO(json.dumps(data))


SESSION = f"process-session:{os.getsid(0)}"

# 製品ごとの、実行の前の入力の実物の形と、Argus へ送る本文の期待。
PRE_CASES = {
    "vscode_copilot": (
        {
            "timestamp": "2026-10-16T00:00:00.000Z",
            "cwd": "/repo",
            "session_id": "vs-1",
            "hook_event_name": "PreToolUse",
            "transcript_path": "/tmp/t.json",
            "tool_name": "runInTerminal",
            "tool_input": {"command": "ls"},
            "tool_use_id": "call-1",
        },
        {
            "session_id": "vs-1",
            "tool_name": "runInTerminal",
            "tool_input": {"command": "ls"},
            "tool_use_id": "call-1",
            "cwd": "/repo",
        },
    ),
    "windsurf": (
        {
            "agent_action_name": "pre_run_command",
            "trajectory_id": "traj-1",
            "execution_id": "exec-1",
            "timestamp": "2026-10-16T00:00:00Z",
            "model_name": "Claude Sonnet 4",
            "tool_info": {"command_line": "npm install x", "cwd": "/repo"},
        },
        {
            "session_id": "traj-1",
            "tool_name": "pre_run_command",
            "tool_input": {"command_line": "npm install x", "cwd": "/repo"},
            "cwd": "/repo",
        },
    ),
    "gemini_cli": (
        {
            "session_id": "g-1",
            "transcript_path": "/tmp/t.json",
            "cwd": "/repo",
            "hook_event_name": "BeforeTool",
            "timestamp": "2026-10-16T00:00:00Z",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "ls"},
            "mcp_context": {},
        },
        {
            "session_id": "g-1",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "ls"},
            "cwd": "/repo",
        },
    ),
    "qwen_code": (
        {
            "session_id": "q-1",
            "transcript_path": "/tmp/t.json",
            "cwd": "/repo",
            "hook_event_name": "PreToolUse",
            "timestamp": "2026-10-16T00:00:00Z",
            "permission_mode": "default",
            "tool_name": "write_file",
            "tool_input": {"file_path": "/repo/a.txt", "content": "x"},
            "tool_use_id": "tu-1",
        },
        {
            "session_id": "q-1",
            "tool_name": "write_file",
            "tool_input": {"file_path": "/repo/a.txt", "content": "x"},
            "tool_use_id": "tu-1",
            "cwd": "/repo",
            "permission_mode": "default",
        },
    ),
    "kiro": (
        {
            "hook_event_name": "PreToolUse",
            "cwd": "/repo",
            "session_id": "k-1",
            "tool_name": "mcp_postgres_query",
            "tool_input": {"sql": "SELECT 1"},
        },
        {
            "session_id": "k-1",
            "tool_name": "mcp_postgres_query",
            "tool_input": {"sql": "SELECT 1"},
            "cwd": "/repo",
        },
    ),
    "amazon_q_cli": (
        {
            "hook_event_name": "preToolUse",
            "cwd": "/repo",
            "tool_name": "@postgres/query",
            "tool_input": {"sql": "SELECT 1"},
        },
        {
            "session_id": SESSION,
            "tool_name": "@postgres/query",
            "tool_input": {"sql": "SELECT 1"},
            "cwd": "/repo",
        },
    ),
    "factory_droid": (
        {
            "session_id": "f-1",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/repo",
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "message_id": "m-1",
            "tool_name": "Execute",
            "tool_input": {"command": "git push"},
        },
        {
            "session_id": "f-1",
            "tool_name": "Execute",
            "tool_input": {"command": "git push"},
            "cwd": "/repo",
            "permission_mode": "default",
        },
    ),
    "crush": (
        {
            "event": "PreToolUse",
            "session_id": "313909e",
            "cwd": "/repo",
            "tool_name": "bash",
            "tool_input": {"command": "ghc main.hs"},
        },
        {
            "session_id": "313909e",
            "tool_name": "bash",
            "tool_input": {"command": "ghc main.hs"},
            "cwd": "/repo",
        },
    ),
    "goose": (
        {
            "event": "PreToolUse",
            "session_id": "abc-123",
            "matcher_context": "shell",
            "tool_name": "shell",
            "tool_input": {"command": "rg TODO"},
            "working_dir": "/repo",
            "tool_call_id": "tc-1",
        },
        {
            "session_id": "abc-123",
            "tool_name": "shell",
            "tool_input": {"command": "rg TODO"},
            "tool_use_id": "tc-1",
            "cwd": "/repo",
        },
    ),
    "augment_code": (
        {
            "hook_event_name": "PreToolUse",
            "conversation_id": "conv-xyz789",
            "workspace_roots": ["/repo"],
            "tool_name": "launch-process",
            "tool_input": {"command": "rm -rf build"},
            "is_mcp_tool": False,
            "context": {"userEmail": "dev@example.com", "modelName": "m"},
        },
        {
            "session_id": "conv-xyz789",
            "tool_name": "launch-process",
            "tool_input": {"command": "rm -rf build"},
            "user_email": "dev@example.com",
        },
    ),
    "junie_cli": (
        # 公式の PreToolUse の入力の例そのまま。session_id と cwd を持たない。
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {
                "command": "sleep 60",
                "run_in_background": False,
                "timeout": 30,
            },
        },
        {
            "session_id": SESSION,
            "tool_name": "Bash",
            "tool_input": {
                "command": "sleep 60",
                "run_in_background": False,
                "timeout": 30,
            },
        },
    ),
    "cline": (
        {
            "clineVersion": "3.0.0",
            "hookName": "PreToolUse",
            "timestamp": "1760000000000",
            "taskId": "task-1",
            "workspaceRoots": ["/repo"],
            "userId": "u-1",
            "preToolUse": {
                "toolName": "write_to_file",
                "parameters": {"path": "a.js", "content": "x"},
            },
        },
        {
            "session_id": "task-1",
            "tool_name": "write_to_file",
            "tool_input": {"path": "a.js", "content": "x"},
        },
    ),
    "antigravity_cli": (
        {
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "npm test", "Cwd": "/repo"},
            },
            "stepIdx": 19,
            "conversationId": "ec33ebf9",
            "workspacePaths": ["/repo"],
            "transcriptPath": "/tmp/t.jsonl",
            "artifactDirectoryPath": "/tmp/a",
            "modelName": "gemini",
        },
        {
            "session_id": "ec33ebf9",
            "tool_name": "run_command",
            "tool_input": {"CommandLine": "npm test", "Cwd": "/repo"},
        },
    ),
}
NEW_PRODUCTS = list(PRE_CASES)

# 製品ごとの、遮断の標準出力と終了コード。標準出力が None の製品は何も書かない。
EXPECTED_DENY = {
    "vscode_copilot": (
        lambda r: {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": r,
            }
        },
        2,
    ),
    "windsurf": (None, 2),
    "gemini_cli": (lambda r: {"decision": "deny", "reason": r}, 2),
    "qwen_code": (
        lambda r: {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": r,
            }
        },
        2,
    ),
    "kiro": (None, 2),
    "amazon_q_cli": (None, 2),
    "factory_droid": (
        lambda r: {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": r,
            }
        },
        2,
    ),
    "crush": (lambda r: {"decision": "deny", "reason": r}, 2),
    "goose": (lambda r: {"decision": "block", "reason": r}, 2),
    "augment_code": (
        lambda r: {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": r,
            }
        },
        2,
    ),
    "junie_cli": (lambda r: {"decision": "deny", "reason": r}, 2),
    "cline": (lambda r: {"cancel": True, "errorMessage": r}, 0),
    "antigravity_cli": (lambda r: {"decision": "deny", "reason": r}, 0),
}
# 許可の標準出力。空文字は何も書かない。
EXPECTED_ALLOW = {
    "cline": {"cancel": False},
    "antigravity_cli": {"decision": "allow"},
}


def test_every_product_has_an_adapter_and_cases():
    assert set(NEW_PRODUCTS) | {"claude_code", "codex", "copilot_cli", "cursor"} == set(
        hooks.PRODUCTS
    )
    assert set(EXPECTED_DENY) == set(NEW_PRODUCTS)
    for product in hooks.PRODUCTS:
        assert adapter_for(product).product == product


@pytest.mark.parametrize("product", NEW_PRODUCTS)
def test_pre_input_is_normalized_to_the_contract_body(trial_env, product):
    data, expected = PRE_CASES[product]
    server = FakeServer([_ok("allow")])
    trial_env.setattr(hooks, "post_json", server)
    out = run_pre(adapter_for(product), _stdin(data), None)
    assert out.exit_code == 0, out.stderr
    url, body = server.calls[0]
    assert url == "http://argus.test/v1/hooks/precheck"
    assert body == {"product": product, "agent_id": "agent-1", **expected}


@pytest.mark.parametrize("product", NEW_PRODUCTS)
def test_deny_is_written_in_the_product_shape(trial_env, product):
    trial_env.setattr(hooks, "post_json", FakeServer([_ok("deny", reason="止める")]))
    out = run_pre(adapter_for(product), _stdin(PRE_CASES[product][0]), None)
    render, code = EXPECTED_DENY[product]
    assert out.exit_code == code
    assert out.stderr == "止める"
    if render is None:
        assert out.stdout == ""
    else:
        assert json.loads(out.stdout) == render("止める")


@pytest.mark.parametrize("product", NEW_PRODUCTS)
def test_pending_is_a_deny_carrying_the_request_id(trial_env, product):
    server = FakeServer([_ok("pending", request_id="req-77", reason="承認を待つ")])
    trial_env.setattr(hooks, "post_json", server)
    out = run_pre(adapter_for(product), _stdin(PRE_CASES[product][0]), None)
    render, code = EXPECTED_DENY[product]
    assert out.exit_code == code
    assert "req-77" in out.stderr
    if render is not None:
        assert "req-77" in out.stdout
        assert json.loads(out.stdout) == render(out.stderr)


@pytest.mark.parametrize("product", NEW_PRODUCTS)
def test_allow_output(trial_env, product):
    trial_env.setattr(hooks, "post_json", FakeServer([_ok("allow")]))
    out = run_pre(adapter_for(product), _stdin(PRE_CASES[product][0]), None)
    assert out.exit_code == 0
    expected = EXPECTED_ALLOW.get(product)
    if expected is None:
        assert out.stdout == ""
    else:
        assert json.loads(out.stdout) == expected


@pytest.mark.parametrize("product", NEW_PRODUCTS)
def test_slow_server_is_denied_by_the_adapter_itself(trial_env, product):
    # 製品の時間切れを待たず、内部の上限でアダプタが遮断を返す。
    trial_env.setenv("SENDA_ARGUS_HOOK_WAIT_LIMIT_SEC", "0.3")

    def hang(url, body, api_key, timeout):
        time.sleep(3)
        return _ok("allow")

    trial_env.setattr(hooks, "post_json", hang)
    started = time.monotonic()
    out = run_pre(adapter_for(product), _stdin(PRE_CASES[product][0]), None)
    assert time.monotonic() - started < 2.0
    render, code = EXPECTED_DENY[product]
    assert out.exit_code == code
    if render is None:
        assert out.stdout == ""
    else:
        assert json.loads(out.stdout) == render(out.stderr)


# 製品ごとの、約束の項目が欠けた入力。読めなければ遮断する。
BROKEN = {
    "vscode_copilot": {"tool_name": "x", "tool_input": {}},
    "windsurf": {"agent_action_name": "pre_run_command", "trajectory_id": "t"},
    "gemini_cli": {"session_id": "s", "tool_input": {}},
    "qwen_code": {"session_id": "s", "tool_name": "x", "tool_input": "not json"},
    "kiro": {"tool_name": "x"},
    "amazon_q_cli": {"cwd": "/repo", "tool_input": {}},
    "factory_droid": {"session_id": "s", "tool_name": "x", "tool_input": [1]},
    "crush": {"session_id": "s"},
    "goose": {"tool_name": "shell"},
    "augment_code": {"tool_name": "x", "tool_input": {}},
    "junie_cli": {"session_id": "s", "tool_input": {}},
    "cline": {"taskId": "t", "toolName": "x", "parameters": {}},
    "antigravity_cli": {"conversationId": "c", "name": "x", "args": {}},
}


@pytest.mark.parametrize("product", NEW_PRODUCTS)
def test_broken_input_is_denied_without_asking_the_server(trial_env, product):
    server = FakeServer([_ok("allow")])
    trial_env.setattr(hooks, "post_json", server)
    out = run_pre(adapter_for(product), _stdin(BROKEN[product]), None)
    assert out.exit_code == EXPECTED_DENY[product][1]
    assert "入力を読めない" in out.stderr
    assert server.calls == []
    out = run_pre(adapter_for(product), io.StringIO("not json"), None)
    assert "入力を読めない" in out.stderr


def test_windsurf_rejects_events_that_are_not_tool_calls(trial_env):
    trial_env.setattr(hooks, "post_json", FakeServer([_ok("allow")]))
    data = {
        "agent_action_name": "pre_user_prompt",
        "trajectory_id": "t",
        "tool_info": {"user_prompt": "hi"},
    }
    out = run_pre(adapter_for("windsurf"), _stdin(data), None)
    assert out.exit_code == 2
    # 実行の後の名前を実行の前に受けない。
    data = dict(PRE_CASES["windsurf"][0], agent_action_name="post_run_command")
    assert run_pre(adapter_for("windsurf"), _stdin(data), None).exit_code == 2


def test_windsurf_post_uses_the_same_name_and_arguments_as_pre(trial_env):
    # サーバーは実行の前と後を tool 名と引数のハッシュで結ぶ。後の hook の名前と結果の項目で変わらない。
    pre = {
        "agent_action_name": "pre_mcp_tool_use",
        "trajectory_id": "traj-9",
        "tool_info": {
            "mcp_server_name": "github",
            "mcp_tool_name": "create_issue",
            "mcp_tool_arguments": {"owner": "o", "repo": "r"},
        },
    }
    post = {
        "agent_action_name": "post_mcp_tool_use",
        "trajectory_id": "traj-9",
        "tool_info": dict(pre["tool_info"], mcp_result="created"),
    }
    server = FakeServer([_ok("allow")])
    trial_env.setattr(hooks, "post_json", server)
    run_pre(adapter_for("windsurf"), _stdin(pre), None)
    run_post(adapter_for("windsurf"), _stdin(post), None)
    pre_body, post_body = server.calls[0][1], dict(server.calls[1][1])
    assert post_body.pop("tool_response") == "created"
    assert post_body == pre_body
    assert pre_body["tool_name"] == "pre_mcp_tool_use"


def test_amazon_q_session_comes_from_the_process_session(trial_env):
    # 入力にセッションの識別子が無い。前と後で同じ値になり、1 つの定数に畳まない。
    server = FakeServer([_ok("allow")])
    trial_env.setattr(hooks, "post_json", server)
    data = PRE_CASES["amazon_q_cli"][0]
    run_pre(adapter_for("amazon_q_cli"), _stdin(data), None)
    run_post(
        adapter_for("amazon_q_cli"),
        _stdin(dict(data, tool_response={"success": True})),
        None,
    )
    assert server.calls[0][1]["session_id"] == server.calls[1][1]["session_id"]
    assert server.calls[0][1]["session_id"] == f"process-session:{os.getsid(0)}"


# 製品ごとの、実行の後の入力と、送る tool_response の期待。
POST_CASES = [
    ("vscode_copilot", {"tool_response": "ok"}, "ok"),
    ("gemini_cli", {"tool_response": {"llmContent": "x"}}, {"llmContent": "x"}),
    ("qwen_code", {"tool_response": {"result": 1}}, {"result": 1}),
    ("qwen_code", {"error": "boom"}, {"error": "boom"}),
    ("kiro", {"tool_response": "done"}, "done"),
    ("amazon_q_cli", {"tool_response": {"success": True}}, {"success": True}),
    ("factory_droid", {"tool_response": "done"}, "done"),
    ("augment_code", {"tool_output": "passed"}, "passed"),
    (
        "augment_code",
        {"tool_error": "permission denied"},
        {"tool_error": "permission denied"},
    ),
    ("goose", {}, None),
]


@pytest.mark.parametrize(("product", "extra", "expected"), POST_CASES)
def test_post_sends_the_result(trial_env, product, extra, expected):
    server = FakeServer([(200, {})])
    trial_env.setattr(hooks, "post_json", server)
    out = run_post(
        adapter_for(product), _stdin({**PRE_CASES[product][0], **extra}), None
    )
    assert out.exit_code == 0
    assert out.stdout == ""
    url, body = server.calls[0]
    assert url == "http://argus.test/v1/hooks/postcall"
    assert body["tool_response"] == expected
    assert body["tool_name"] == PRE_CASES[product][1]["tool_name"]


def test_cline_post_reads_the_post_container(trial_env):
    server = FakeServer([(200, {})])
    trial_env.setattr(hooks, "post_json", server)
    data = {
        "hookName": "PostToolUse",
        "taskId": "task-1",
        "postToolUse": {
            "toolName": "write_to_file",
            "parameters": {"path": "a.js"},
            "result": "written",
            "success": True,
            "executionTimeMs": 3,
        },
    }
    run_post(adapter_for("cline"), _stdin(data), None)
    body = server.calls[0][1]
    assert body["tool_name"] == "write_to_file"
    assert body["tool_input"] == {"path": "a.js"}
    assert body["tool_response"] == "written"


def test_antigravity_post_carries_the_error(trial_env):
    server = FakeServer([(200, {})])
    trial_env.setattr(hooks, "post_json", server)
    data = dict(PRE_CASES["antigravity_cli"][0], error="exit status 1")
    run_post(adapter_for("antigravity_cli"), _stdin(data), None)
    assert server.calls[0][1]["tool_response"] == {"error": "exit status 1"}


@pytest.mark.parametrize("product", NEW_PRODUCTS)
def test_cli_hook_entry_accepts_every_product(trial_env, monkeypatch, capsys, product):
    from senda_argus_hooks.cli.main import main

    trial_env.setattr(hooks, "post_json", FakeServer([_ok("deny", reason="止める")]))
    monkeypatch.setattr("sys.stdin", _stdin(PRE_CASES[product][0]))
    code = main(["hook", product, "pre"])
    captured = capsys.readouterr()
    assert code == EXPECTED_DENY[product][1]
    assert "止める" in captured.err


def test_junie_session_id_is_used_when_present(trial_env):
    server = FakeServer([_ok("allow")])
    trial_env.setattr(hooks, "post_json", server)
    data = dict(PRE_CASES["junie_cli"][0], session_id="j-1", cwd="/repo")
    run_pre(adapter_for("junie_cli"), _stdin(data), None)
    assert server.calls[0][1]["session_id"] == "j-1"
    assert server.calls[0][1]["cwd"] == "/repo"


def test_products_with_a_session_field_do_not_fall_back_to_the_process(trial_env):
    # session の項目を必ず持つ製品で欠けた入力は、プロセスのセッションで埋めずに遮断する。
    server = FakeServer([_ok("allow")])
    trial_env.setattr(hooks, "post_json", server)
    data = dict(PRE_CASES["gemini_cli"][0])
    data.pop("session_id")
    out = run_pre(adapter_for("gemini_cli"), _stdin(data), None)
    assert out.exit_code == 2
    assert server.calls == []


@pytest.mark.parametrize(
    ("product", "data", "expected"),
    [
        (
            "cline",
            {
                "hookName": "PostToolUse",
                "taskId": "t",
                "postToolUse": {"toolName": "x", "parameters": {}, "result": "ok"},
            },
            {"cancel": False},
        ),
        ("antigravity_cli", PRE_CASES["antigravity_cli"][0], {}),
    ],
)
def test_post_output_follows_the_product_contract(trial_env, product, data, expected):
    # 全ての hook が応答を返すと公式に書かれた製品は、実行の後でも応答を書く。送れなくても書く。
    trial_env.setattr(hooks, "post_json", FakeServer([hooks.TransportError("x")]))
    out = run_post(adapter_for(product), _stdin(data), None)
    assert out.exit_code == 0
    assert json.loads(out.stdout) == expected


def test_augment_null_output_does_not_hide_the_error(trial_env):
    server = FakeServer([(200, {})])
    trial_env.setattr(hooks, "post_json", server)
    data = dict(PRE_CASES["augment_code"][0], tool_output=None, tool_error="denied")
    run_post(adapter_for("augment_code"), _stdin(data), None)
    assert server.calls[0][1]["tool_response"] == {"tool_error": "denied"}
