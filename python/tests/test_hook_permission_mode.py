"""製品の入力の実行モード permission_mode を、判定の要求の本文へ載せるテスト。

実行モードを持つと公式の文書に書かれた 4 製品だけが送る。ほかの製品の入力に同じ名前の項目があっても
送らない。値の意味は読まず、上限で切るだけにする。入力は各製品の公式の文書の例と同じ形で書く。
"""

from __future__ import annotations

import io
import json

import pytest

from senda_argus_hooks.integrations import coding_agent_hooks as hooks
from senda_argus_hooks.integrations.coding_agent_hooks import (
    PERMISSION_MODE_MAX_CHARS,
    PERMISSION_MODE_PRODUCTS,
    adapter_for,
    run_post,
    run_pre,
)


class FakeServer:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, body, api_key, timeout):
        self.calls.append((url, json.loads(json.dumps(body))))
        return (
            200,
            {"decision": "allow", "request_id": None, "reason": "", "wait_hint_sec": 0},
        )


@pytest.fixture
def server(monkeypatch):
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
    fake = FakeServer()
    monkeypatch.setattr(hooks, "post_json", fake)
    return fake


# 製品ごとの実行の前の入力。permission_mode は各テストで差し替える。
PRE_INPUTS = {
    "claude_code": {
        "session_id": "s-1",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "/repo/a.txt", "content": "x"},
        "tool_use_id": "toolu_01",
    },
    "codex": {
        "session_id": "cx-1",
        "transcript_path": None,
        "cwd": "/repo",
        "hook_event_name": "PreToolUse",
        "model": "gpt-5",
        "turn_id": "t-1",
        "tool_name": "apply_patch",
        "tool_use_id": "call_9",
        "tool_input": {"command": "*** Begin Patch"},
    },
    "qwen_code": {
        "session_id": "q-1",
        "transcript_path": "/tmp/t.json",
        "cwd": "/repo",
        "hook_event_name": "PreToolUse",
        "timestamp": "2026-10-16T00:00:00Z",
        "tool_name": "write_file",
        "tool_input": {"file_path": "/repo/a.txt", "content": "x"},
        "tool_use_id": "tu-1",
    },
    "factory_droid": {
        "session_id": "f-1",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PreToolUse",
        "message_id": "m-1",
        "tool_name": "Create",
        "tool_input": {"file_path": "/repo/a.txt", "content": "x"},
    },
    # 実行モードの項目の有無を文書で断定できない製品。実際の入力で確認するまで読まない。
    "kiro": {
        "hook_event_name": "PreToolUse",
        "cwd": "/repo",
        "session_id": "k-1",
        "tool_name": "fs_write",
        "tool_input": {"path": "/repo/a.txt"},
    },
    "junie_cli": {
        "hook_event_name": "PreToolUse",
        "cwd": "/repo",
        "session_id": "j-1",
        "tool_name": "Write",
        "tool_input": {"path": "/repo/a.txt"},
    },
    # 実行モードの項目を持たない製品。
    "gemini_cli": {
        "session_id": "g-1",
        "cwd": "/repo",
        "hook_event_name": "BeforeTool",
        "tool_name": "write_file",
        "tool_input": {"file_path": "/repo/a.txt"},
    },
    "crush": {
        "event": "PreToolUse",
        "session_id": "313909e",
        "cwd": "/repo",
        "tool_name": "write",
        "tool_input": {"file_path": "/repo/a.txt"},
    },
}

WITH_MODE = sorted(PERMISSION_MODE_PRODUCTS)
WITHOUT_MODE = sorted(set(PRE_INPUTS) - PERMISSION_MODE_PRODUCTS)


def _run(product, data):
    return run_pre(adapter_for(product), io.StringIO(json.dumps(data)), None)


def test_the_products_that_send_the_mode_are_the_four_documented_ones():
    assert PERMISSION_MODE_PRODUCTS == {
        "claude_code",
        "codex",
        "qwen_code",
        "factory_droid",
    }
    # サーバーの受け取りの上限と同じ値。超える値はサーバーで読めない値として扱われる。
    assert PERMISSION_MODE_MAX_CHARS == 64


@pytest.mark.parametrize("product", WITH_MODE)
def test_the_mode_in_the_product_input_is_sent(server, product):
    out = _run(product, {**PRE_INPUTS[product], "permission_mode": "plan"})
    assert out.exit_code == 0, out.stderr
    assert server.calls[0][1]["permission_mode"] == "plan"


@pytest.mark.parametrize("product", WITH_MODE)
def test_the_mode_is_not_sent_when_the_input_has_no_mode(server, product):
    _run(product, PRE_INPUTS[product])
    assert "permission_mode" not in server.calls[0][1]


@pytest.mark.parametrize("product", WITHOUT_MODE)
def test_a_product_without_a_documented_mode_does_not_send_it(server, product):
    out = _run(product, {**PRE_INPUTS[product], "permission_mode": "plan"})
    assert out.exit_code == 0, out.stderr
    assert "permission_mode" not in server.calls[0][1]


@pytest.mark.parametrize("value", [123, None, "", ["plan"], {"mode": "plan"}, True])
def test_a_mode_that_is_not_a_non_empty_string_is_not_sent(server, value):
    _run("claude_code", {**PRE_INPUTS["claude_code"], "permission_mode": value})
    assert "permission_mode" not in server.calls[0][1]


def test_a_long_mode_is_cut_to_the_limit(server):
    value = "p" * (PERMISSION_MODE_MAX_CHARS * 3)
    _run("claude_code", {**PRE_INPUTS["claude_code"], "permission_mode": value})
    sent = server.calls[0][1]["permission_mode"]
    assert sent == "p" * PERMISSION_MODE_MAX_CHARS


def test_the_value_is_sent_as_is_without_reading_its_meaning(server):
    # 画面で Manual と表示されるモードは default で届く。アダプタは値を読み替えない。
    _run("claude_code", {**PRE_INPUTS["claude_code"], "permission_mode": "Manual"})
    assert server.calls[0][1]["permission_mode"] == "Manual"


def test_the_post_call_carries_the_mode_of_that_call(server):
    data = {
        **PRE_INPUTS["claude_code"],
        "hook_event_name": "PostToolUse",
        "permission_mode": "acceptEdits",
        "tool_response": {"success": True},
    }
    run_post(adapter_for("claude_code"), io.StringIO(json.dumps(data)), None)
    assert server.calls[0][1]["permission_mode"] == "acceptEdits"
