"""実行前の判定を受けて、ツールの呼び出しを止めることを見る。"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core import precheck
from senda_argus_hooks.core.precheck import ToolCallBlocked, check_tool_call, configure_precheck
from senda_argus_hooks.integrations.langchain import SendaArgusCallbackHandler

COMMAND = "useradd evil"


@pytest.fixture
def server():
    state = {"decision": "deny", "bodies": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            payload = b"{}"
            if self.path == precheck.PRECHECK_PATH:
                state["bodies"].append(json.loads(raw))
                payload = json.dumps({"decision": state["decision"], "reason": "held"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_port}"
    yield state
    httpd.shutdown()
    configure_precheck(None)


def _register(tmp_path, url: str | None, **kw) -> None:
    register(project="t", exporters=[{"type": "jsonl", "path": str(tmp_path / "e.jsonl")}], auto_instrument=False, **kw)
    configure_precheck(url)


def test_a_denied_call_raises(tmp_path, server):
    _register(tmp_path, server["url"], enforce_precheck=True)
    try:
        with pytest.raises(ToolCallBlocked) as caught:
            check_tool_call("run", {"cmd": COMMAND}, mcp_server="demo")
    finally:
        shutdown()
    assert caught.value.decision == "deny"
    body = server["bodies"][0]
    assert body["product"] == "sdk"
    assert body["mcp_server"] == "demo"
    assert COMMAND in body["tool_input"]["arguments"]


def test_a_pending_call_raises(tmp_path, server):
    server["decision"] = "pending"
    _register(tmp_path, server["url"], enforce_precheck=True)
    try:
        with pytest.raises(ToolCallBlocked):
            check_tool_call("run", {})
    finally:
        shutdown()


def test_an_allowed_call_passes(tmp_path, server):
    server["decision"] = "allow"
    _register(tmp_path, server["url"], enforce_precheck=True)
    try:
        check_tool_call("run", {})
    finally:
        shutdown()


def test_it_is_off_by_default(tmp_path, server):
    _register(tmp_path, server["url"])
    try:
        check_tool_call("run", {})
    finally:
        shutdown()
    assert server["bodies"] == []


def test_the_check_reads_arguments_even_when_the_scan_is_off(tmp_path, server):
    """送らないと内容を判定できず、1 回の承認が全ての引数に効く。"""
    server["decision"] = "allow"
    _register(tmp_path, server["url"], enforce_precheck=True, scan_arguments=False)
    try:
        check_tool_call("run", {"cmd": COMMAND})
    finally:
        shutdown()
    assert COMMAND in server["bodies"][0]["tool_input"]["arguments"]


def test_a_huge_argument_is_reported_unreadable_with_its_full_digest(tmp_path, server):
    server["decision"] = "allow"
    _register(tmp_path, server["url"], enforce_precheck=True)
    pad = "x" * 120000
    try:
        check_tool_call("run", {"a": pad, "c": "first", "b": pad})
        check_tool_call("run", {"a": pad, "c": "other", "b": pad})
    finally:
        shutdown()
    one, two = (b["tool_input"] for b in server["bodies"])
    assert one["arguments_unreadable"] is True
    assert one["arguments_sha256"] != two["arguments_sha256"]


def test_a_lone_surrogate_cannot_skip_the_check(tmp_path, server):
    _register(tmp_path, server["url"], enforce_precheck=True)
    try:
        with pytest.raises(ToolCallBlocked):
            check_tool_call("run", {"a": "\ud800", "b": COMMAND})
    finally:
        shutdown()


def test_a_rejected_key_blocks_even_when_fail_open(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(401)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _register(tmp_path, f"http://127.0.0.1:{httpd.server_port}", enforce_precheck=True)
    try:
        with pytest.raises(ToolCallBlocked) as caught:
            check_tool_call("run", {})
    finally:
        shutdown()
        httpd.shutdown()
        configure_precheck(None)
    assert caught.value.decision == "rejected"


def test_a_handler_made_before_register_follows_the_setting(tmp_path, server):
    _register(tmp_path, server["url"], enforce_precheck=False)
    handler = SendaArgusCallbackHandler()
    assert handler.raise_error is False
    shutdown()
    _register(tmp_path, server["url"], enforce_precheck=True)
    try:
        assert handler.raise_error is True
    finally:
        shutdown()


def test_an_observation_failure_does_not_stop_the_agent(tmp_path, server, monkeypatch):
    from senda_argus_hooks.integrations import langchain as module

    _register(tmp_path, server["url"], enforce_precheck=True)
    monkeypatch.setattr(module, "_latency_ms", lambda *_: 1 / 0)
    try:
        SendaArgusCallbackHandler().on_tool_end("ok", run_id="r1")
    finally:
        shutdown()


@pytest.mark.parametrize(("fail_open", "raises"), [(True, False), (False, True)])
def test_an_unreachable_server_follows_the_setting(tmp_path, fail_open, raises):
    _register(tmp_path, "http://127.0.0.1:9", enforce_precheck=True, precheck_fail_open=fail_open)
    try:
        if raises:
            with pytest.raises(ToolCallBlocked):
                check_tool_call("run", {})
        else:
            check_tool_call("run", {})
    finally:
        shutdown()
        configure_precheck(None)


def test_langchain_raises_only_when_enforced(tmp_path, server):
    _register(tmp_path, server["url"], enforce_precheck=True)
    try:
        handler = SendaArgusCallbackHandler()
        assert handler.raise_error is True
        with pytest.raises(ToolCallBlocked):
            handler.on_tool_start({"name": "run"}, COMMAND, run_id="r1")
    finally:
        shutdown()


def test_a_denied_mcp_call_does_not_run_the_tool(tmp_path, server, monkeypatch):
    import sys
    import types

    ran: list[str] = []

    class ClientSession:
        server = "fake_mcp"

        async def call_tool(self, name, arguments=None, **kwargs):
            ran.append(name)
            return {"content": [], "isError": False}

        async def list_tools(self):
            return []

    fake = types.ModuleType("mcp")
    fake.ClientSession = ClientSession
    monkeypatch.setitem(sys.modules, "mcp", fake)
    register(
        project="t",
        exporters=[{"type": "jsonl", "path": str(tmp_path / "e.jsonl")}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_ollama=False,
        instrument_bedrock=False,
        instrument_vertexai=False,
        instrument_openai_agents=False,
        instrument_argus_sdk=False,
        enforce_precheck=True,
    )
    configure_precheck(server["url"])
    try:
        with pytest.raises(ToolCallBlocked):
            asyncio.run(ClientSession().call_tool("run", {"cmd": COMMAND}))
    finally:
        shutdown()
    assert ran == []
