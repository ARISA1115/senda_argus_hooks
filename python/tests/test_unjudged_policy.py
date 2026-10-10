"""Argus が判定できない時の扱いの試験。

偽の Argus をテストの中で立て、外部へは通信しない。
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from senda_argus_hooks.core import unjudged as uj
from senda_argus_hooks.core.unjudged import (
    AdmissionGuard,
    UnjudgedActionBlocked,
    clamp_ttl,
    parse_policy,
)
from senda_argus_hooks.exporters.argus import ArgusExporter

ALL_LOCAL = {"actions_exhausted": "block", "quota_unknown": "hold", "read_only": "block", "argus_unavailable": "pass"}


class _FakeArgus(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, spec):
        status, body, headers = spec
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        srv = self.server
        with srv.lock:
            srv.admission_calls += 1
            srv.admission_keys.append(self.headers.get("X-API-Key"))
            spec = srv.admission[0] if len(srv.admission) == 1 else srv.admission.pop(0)
        self._reply(spec)

    def do_POST(self):
        srv = self.server
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        with srv.lock:
            srv.ingest_calls += 1
            srv.captured.append(body)
            spec = srv.ingest[0] if len(srv.ingest) == 1 else srv.ingest.pop(0)
        self._reply(spec)


@pytest.fixture
def fake_argus():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeArgus)
    httpd.lock = threading.Lock()
    httpd.admission = [(200, {"admitted": True, "reason_code": None, "action": None, "ttl_seconds": 60}, None)]
    httpd.ingest = [(200, {"accepted": 1}, None)]
    httpd.admission_calls = 0
    httpd.admission_keys = []
    httpd.ingest_calls = 0
    httpd.captured = []
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def _reset_guard():
    uj.set_guard(None)
    yield
    uj.set_guard(None)


def _closed_port_endpoint() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def _not_admitted(reason, action=None, policy=None, ttl=60):
    body = {"admitted": False, "reason_code": reason, "action": action, "ttl_seconds": ttl}
    if policy is not None:
        body["unjudged_policy"] = policy
    return (200, body, None)


def _guard(endpoint, *, policy=None, hold_seconds=0.3, records=None, **kwargs):
    def emit(event_type, **kw):
        if records is not None:
            records.append((event_type, kw))

    return AdmissionGuard(
        endpoint,
        "test-key",
        local_policy=policy if policy is not None else dict(ALL_LOCAL),
        hold_seconds=hold_seconds,
        hold_poll=0.05,
        emit=emit,
        **kwargs,
    )


# 方針の読み込み -------------------------------------------------------------


def test_parse_policy_defaults():
    assert parse_policy(None) == ALL_LOCAL
    assert parse_policy("") == ALL_LOCAL


def test_parse_policy_overrides_and_drops_only_unknown_items(caplog):
    caplog.set_level(logging.WARNING)
    policy = parse_policy("actions_exhausted=pass, bogus=block, quota_unknown=deny, read_only=hold, argus_unavailable")
    assert policy == {
        "actions_exhausted": "pass",
        "quota_unknown": "hold",
        "read_only": "hold",
        "argus_unavailable": "pass",
    }
    text = caplog.text
    assert "bogus=block" in text
    assert "quota_unknown=deny" in text
    assert "argus_unavailable" in text


def test_clamp_ttl_bounds():
    assert clamp_ttl(0) == 1.0
    assert clamp_ttl(-5) == 1.0
    assert clamp_ttl(10_000) == 300.0
    assert clamp_ttl(42) == 42.0
    assert clamp_ttl("x") == 60.0


# 符号ごとの扱い ---------------------------------------------------------------


@pytest.mark.parametrize("reason", ["actions_exhausted", "quota_unknown", "read_only"])
def test_block_per_reason_from_local_policy(fake_argus, reason):
    httpd, endpoint = fake_argus
    httpd.admission = [_not_admitted(reason)]
    records = []
    guard = _guard(endpoint, policy={**ALL_LOCAL, reason: "block"}, records=records)
    with pytest.raises(UnjudgedActionBlocked) as info:
        guard.before_action("lookup")
    assert info.value.reason_code == reason
    assert info.value.action == "block"
    assert records[0][0] == "argus.unjudged"
    assert records[0][1]["data"]["unjudged"]["action"] == "block"


@pytest.mark.parametrize("reason", ["actions_exhausted", "quota_unknown", "read_only"])
def test_pass_per_reason_from_local_policy(fake_argus, reason, caplog):
    caplog.set_level(logging.WARNING)
    httpd, endpoint = fake_argus
    httpd.admission = [_not_admitted(reason)]
    records = []
    guard = _guard(endpoint, policy={**ALL_LOCAL, reason: "pass"}, records=records)
    marker = guard.before_action("lookup")
    assert marker is not None
    assert marker.as_fields() == {"unjudged": True, "unjudged_reason": reason, "unjudged_action": "pass"}
    assert f"reason_code={reason}" in caplog.text
    data = records[0][1]["data"]["unjudged"]
    assert set(data) == {"reason_code", "action", "at", "tool_name_hash"}
    assert data["reason_code"] == reason
    assert data["action"] == "pass"
    assert data["tool_name_hash"] != "lookup"


@pytest.mark.parametrize("reason", ["actions_exhausted", "quota_unknown", "read_only"])
def test_hold_per_reason_stops_at_limit(fake_argus, reason):
    httpd, endpoint = fake_argus
    httpd.admission = [_not_admitted(reason)]
    records = []
    guard = _guard(endpoint, policy={**ALL_LOCAL, reason: "hold"}, hold_seconds=0.3, records=records)
    started = time.monotonic()
    with pytest.raises(UnjudgedActionBlocked) as info:
        guard.before_action("lookup")
    elapsed = time.monotonic() - started
    assert info.value.reason_code == reason
    assert info.value.action == "hold"
    assert elapsed >= 0.3
    # 上限まで照会を繰り返した。
    assert httpd.admission_calls >= 3
    assert records[0][1]["data"]["unjudged"]["action"] == "hold"


def test_hold_proceeds_when_judgment_becomes_available(fake_argus):
    httpd, endpoint = fake_argus
    admitted = (200, {"admitted": True, "ttl_seconds": 60}, None)
    httpd.admission = [_not_admitted("quota_unknown"), _not_admitted("quota_unknown"), admitted]
    records = []
    guard = _guard(endpoint, hold_seconds=5.0, records=records)
    assert guard.before_action("lookup") is None
    assert httpd.admission_calls == 3
    # 判定ができたため、判定を省いた記録は積まない。
    assert records == []


def test_hold_async_proceeds_when_judgment_becomes_available(fake_argus):
    httpd, endpoint = fake_argus
    admitted = (200, {"admitted": True, "ttl_seconds": 60}, None)
    httpd.admission = [_not_admitted("quota_unknown"), admitted]
    guard = _guard(endpoint, hold_seconds=5.0)
    assert asyncio.run(guard.before_action_async("lookup")) is None
    assert httpd.admission_calls == 2


def test_hold_async_stops_at_limit(fake_argus):
    httpd, endpoint = fake_argus
    httpd.admission = [_not_admitted("quota_unknown")]
    guard = _guard(endpoint, hold_seconds=0.2)
    with pytest.raises(UnjudgedActionBlocked) as info:
        asyncio.run(guard.before_action_async("lookup"))
    assert info.value.action == "hold"


def test_argus_unavailable_default_is_pass_with_warning_and_record(caplog):
    caplog.set_level(logging.WARNING)
    records = []
    guard = AdmissionGuard(
        _closed_port_endpoint(), "k", local_policy=parse_policy(None), hold_seconds=0.1, emit=lambda t, **kw: records.append((t, kw))
    )
    marker = guard.before_action("lookup")
    assert marker is not None
    assert marker.reason_code == "argus_unavailable"
    assert marker.action == "pass"
    assert "reason_code=argus_unavailable" in caplog.text
    assert records[0][0] == "argus.unjudged"
    assert records[0][1]["data"]["unjudged"]["reason_code"] == "argus_unavailable"


def test_argus_5xx_is_unavailable(fake_argus):
    httpd, endpoint = fake_argus
    httpd.admission = [(500, {"error": "boom"}, None)]
    guard = _guard(endpoint, policy={**ALL_LOCAL, "argus_unavailable": "block"})
    with pytest.raises(UnjudgedActionBlocked) as info:
        guard.before_action("lookup")
    assert info.value.reason_code == "argus_unavailable"


def test_server_policy_takes_precedence_over_local(fake_argus):
    httpd, endpoint = fake_argus
    server_policy = {**ALL_LOCAL, "actions_exhausted": "block"}
    httpd.admission = [_not_admitted("actions_exhausted", policy=server_policy)]
    guard = _guard(endpoint, policy={**ALL_LOCAL, "actions_exhausted": "pass"})
    with pytest.raises(UnjudgedActionBlocked) as info:
        guard.before_action("lookup")
    assert info.value.action == "block"


def test_last_server_policy_used_when_argus_unreachable(fake_argus):
    httpd, endpoint = fake_argus
    server_policy = {**ALL_LOCAL, "argus_unavailable": "block"}
    httpd.admission = [(200, {"admitted": True, "unjudged_policy": server_policy, "ttl_seconds": 1}, None)]
    guard = _guard(endpoint, policy={**ALL_LOCAL, "argus_unavailable": "pass"})
    assert guard.before_action("lookup") is None
    httpd.admission = [(503, {"error": "down"}, None)]
    # 控えの期限を待たずに照会し直し、受け取り側に届かない状態にする。
    guard.admission(refresh=True)
    with pytest.raises(UnjudgedActionBlocked) as info:
        guard.before_action("lookup")
    assert info.value.reason_code == "argus_unavailable"
    assert info.value.action == "block"


def test_admission_cached_for_ttl_and_sends_key(fake_argus):
    httpd, endpoint = fake_argus
    httpd.admission = [(200, {"admitted": True, "ttl_seconds": 0}, None)]
    guard = _guard(endpoint)
    guard.before_action("a")
    guard.before_action("b")
    # ttl_seconds 0 は 1 秒に丸め、その間は照会しない。
    assert httpd.admission_calls == 1
    assert httpd.admission_keys == ["test-key"]


def test_legacy_receiver_404_is_admitted_and_logged_once(fake_argus, caplog):
    caplog.set_level(logging.INFO)
    httpd, endpoint = fake_argus
    httpd.admission = [(404, {"error": "not found"}, None)]
    records = []
    guard = _guard(endpoint, records=records)
    assert guard.before_action("a") is None
    guard.admission(refresh=True)
    assert records == []
    assert caplog.text.count("判定の可否の照会を持たない") == 1


def test_403_read_only_maps_to_read_only(fake_argus):
    httpd, endpoint = fake_argus
    httpd.admission = [(403, {"error_code": "organization_read_only", "retryable": False}, None)]
    guard = _guard(endpoint)
    with pytest.raises(UnjudgedActionBlocked) as info:
        guard.before_action("a")
    assert info.value.reason_code == "read_only"
    assert info.value.action == "block"


@pytest.mark.parametrize("status,body", [(403, {"error_code": "organization_suspended", "retryable": False}), (401, {"error": "invalid key"})])
def test_other_401_403_map_to_argus_unavailable(fake_argus, status, body):
    httpd, endpoint = fake_argus
    httpd.admission = [(status, body, None)]
    guard = _guard(endpoint)
    marker = guard.before_action("a")
    assert marker is not None
    assert marker.reason_code == "argus_unavailable"
    assert marker.action == "pass"


# 行動の実行の場所 -------------------------------------------------------------


def _install_fake_mcp(monkeypatch, calls):
    class ClientSession:
        server = "fake_mcp"

        async def call_tool(self, name, arguments=None, **kwargs):
            calls.append(name)
            return {"ok": True}

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = ClientSession
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    return ClientSession


def _register_argus(endpoint, **kwargs):
    from senda_argus_hooks import register

    return register(
        exporters=[{"type": "argus", "endpoint": endpoint, "api_key": "test-key"}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_ollama=False,
        instrument_bedrock=False,
        instrument_vertexai=False,
        instrument_openai_agents=False,
        instrument_openai_realtime=False,
        instrument_model_loading=False,
        **kwargs,
    )


def _all_events(httpd):
    return [ev for body in httpd.captured for ev in body.get("events", [])]


def _wait_events(httpd, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(_all_events(httpd)):
            return
        time.sleep(0.02)


def test_mcp_python_call_tool_block_does_not_run_tool(fake_argus, monkeypatch):
    from senda_argus_hooks import shutdown

    httpd, endpoint = fake_argus
    httpd.admission = [_not_admitted("actions_exhausted")]
    calls = []
    ClientSession = _install_fake_mcp(monkeypatch, calls)
    _register_argus(endpoint)
    try:
        with pytest.raises(UnjudgedActionBlocked) as info:
            asyncio.run(ClientSession().call_tool("lookup", {"q": "secret prompt"}))
        assert calls == []
        assert info.value.reason_code == "actions_exhausted"
        assert info.value.action == "block"
        _wait_events(httpd, lambda evs: any(e["event_type"] == "argus.unjudged" for e in evs))
        records = [e for e in _all_events(httpd) if e["event_type"] == "argus.unjudged"]
        assert len(records) == 1
        assert set(records[0]["data"]) == {"unjudged"}
        assert set(records[0]["data"]["unjudged"]) == {"reason_code", "action", "at", "tool_name_hash"}
        assert "secret prompt" not in json.dumps(records[0])
    finally:
        shutdown()


def test_mcp_python_call_tool_pass_runs_and_marks(fake_argus, monkeypatch):
    from senda_argus_hooks import shutdown

    httpd, endpoint = fake_argus
    httpd.admission = [(503, {"error": "down"}, None)]
    calls = []
    ClientSession = _install_fake_mcp(monkeypatch, calls)
    _register_argus(endpoint)
    try:
        asyncio.run(ClientSession().call_tool("lookup", {"q": 1}))
        assert calls == ["lookup"]
        _wait_events(httpd, lambda evs: any(e["event_type"] == "mcp.tool_call.completed" for e in evs))
        events = _all_events(httpd)
        requested = [e for e in events if e["event_type"] == "mcp.tool_call.requested"]
        assert requested[0]["data"]["mcp"]["unjudged"] is True
        assert requested[0]["data"]["mcp"]["unjudged_reason"] == "argus_unavailable"
        assert any(e["event_type"] == "argus.unjudged" for e in events)
    finally:
        shutdown()


def test_mcp_python_call_tool_admitted_runs_without_marker(fake_argus, monkeypatch):
    from senda_argus_hooks import shutdown

    httpd, endpoint = fake_argus
    calls = []
    ClientSession = _install_fake_mcp(monkeypatch, calls)
    _register_argus(endpoint)
    try:
        asyncio.run(ClientSession().call_tool("lookup", {}))
        assert calls == ["lookup"]
        assert httpd.admission_calls == 1
        _wait_events(httpd, lambda evs: any(e["event_type"] == "mcp.tool_call.completed" for e in evs))
        events = _all_events(httpd)
        assert not any(e["event_type"] == "argus.unjudged" for e in events)
        requested = [e for e in events if e["event_type"] == "mcp.tool_call.requested"]
        assert "unjudged" not in requested[0]["data"]["mcp"]
    finally:
        shutdown()


def test_guard_off_disables_check_and_warns_once(fake_argus, monkeypatch, caplog):
    from senda_argus_hooks import shutdown

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(uj, "_disabled_warned", False)
    monkeypatch.setenv("SENDA_ARGUS_UNJUDGED_GUARD", "off")
    httpd, endpoint = fake_argus
    httpd.admission = [_not_admitted("actions_exhausted")]
    calls = []
    ClientSession = _install_fake_mcp(monkeypatch, calls)
    _register_argus(endpoint)
    try:
        asyncio.run(ClientSession().call_tool("lookup", {}))
        assert calls == ["lookup"]
        assert httpd.admission_calls == 0
    finally:
        shutdown()
    _register_argus(endpoint)
    shutdown()
    assert caplog.text.count("SENDA_ARGUS_UNJUDGED_GUARD=off") == 1


def test_guard_not_enabled_without_argus_exporter(tmp_path):
    from senda_argus_hooks import register, shutdown

    register(exporters=[{"type": "jsonl", "path": str(tmp_path / "e.jsonl")}], auto_instrument=False)
    try:
        assert uj.get_guard() is None
    finally:
        shutdown()


def test_guard_reads_local_policy_from_env(fake_argus, monkeypatch):
    from senda_argus_hooks import shutdown

    monkeypatch.setenv("SENDA_ARGUS_UNJUDGED_POLICY", "actions_exhausted=pass")
    _, endpoint = fake_argus
    from senda_argus_hooks import register

    register(exporters=[{"type": "argus", "endpoint": endpoint, "api_key": "k"}], auto_instrument=False)
    try:
        guard = uj.get_guard()
        assert guard is not None
        assert guard.policy()["actions_exhausted"] == "pass"
    finally:
        shutdown()


def test_sdk_mock_mcp_block_does_not_run_tool(fake_argus):
    from senda_argus_hooks import shutdown
    from senda_argus_hooks.sdk import MockMCPClient

    httpd, endpoint = fake_argus
    httpd.admission = [_not_admitted("read_only")]
    calls = []
    client = MockMCPClient({"lookup": lambda **kw: calls.append(kw) or "ok"})
    _register_argus(endpoint, instrument_mcp=False)
    try:
        with pytest.raises(UnjudgedActionBlocked) as info:
            client.call_tool("lookup", {"x": 1})
        assert calls == []
        assert info.value.reason_code == "read_only"
    finally:
        shutdown()


def test_audit_mcp_tool_call_block_does_not_run_body(fake_argus):
    from senda_argus_hooks import audit, register, shutdown
    from senda_argus_hooks.register import _configure_unjudged_guard

    # 判定を省いた記録の送り先を、前の試験の送出器でなく何もしない送出器にする。
    register(exporters=[{"type": "null"}], auto_instrument=False)

    httpd, endpoint = fake_argus
    httpd.admission = [_not_admitted("actions_exhausted")]
    _configure_unjudged_guard([ArgusExporter({"endpoint": endpoint, "api_key": "k"})])
    ran = []
    with pytest.raises(UnjudgedActionBlocked):
        with audit.mcp_tool_call(server="s", tool="t", arguments={}):
            ran.append(1)
    assert ran == []
    shutdown()


# 送出の側 -------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["actions_exhausted", "quota_unknown", "organization_read_only"])
def test_send_sync_terminal_unjudged_codes_advance_and_warn(fake_argus, code, caplog):
    caplog.set_level(logging.WARNING)
    httpd, endpoint = fake_argus
    httpd.ingest = [(403, {"error_code": code, "unjudged_action": "block", "retryable": False}, None)]
    exporter = ArgusExporter({"endpoint": endpoint, "api_key": "k"})
    assert exporter.send_sync([{"event_id": "e1", "event_type": "x"}]) is True
    assert code in caplog.text


def test_send_sync_hold_503_is_not_advanced(fake_argus):
    httpd, endpoint = fake_argus
    httpd.ingest = [(503, {"error_code": "quota_unknown", "unjudged_action": "hold", "retryable": True}, {"Retry-After": "1"})]
    exporter = ArgusExporter({"endpoint": endpoint, "api_key": "k"})
    assert exporter.send_sync([{"event_id": "e1", "event_type": "x"}]) is False


def test_send_sync_accepted_without_judgment_warns(fake_argus, caplog):
    caplog.set_level(logging.WARNING)
    httpd, endpoint = fake_argus
    httpd.ingest = [(202, {"accepted": 0, "judged": False, "unjudged_reason": "argus_unavailable", "unjudged_action": "pass", "retryable": False}, None)]
    exporter = ArgusExporter({"endpoint": endpoint, "api_key": "k"})
    assert exporter.send_sync([{"event_id": "e1", "event_type": "x"}]) is True
    assert "判定を省いて記録を受け付けました: argus_unavailable" in caplog.text


def test_export_terminal_code_is_not_retried_and_warns(fake_argus, caplog):
    caplog.set_level(logging.WARNING)
    httpd, endpoint = fake_argus
    httpd.ingest = [(403, {"error_code": "actions_exhausted", "retryable": False}, None)]
    exporter = ArgusExporter({"endpoint": endpoint, "api_key": "k", "retry_max_attempts": 3, "retry_base_delay": 0})
    exporter.export([{"event_id": "e1", "event_type": "x"}])
    exporter.shutdown()
    assert httpd.ingest_calls == 1
    assert "送り直しません: actions_exhausted" in caplog.text


def test_export_hold_503_is_retried(fake_argus):
    httpd, endpoint = fake_argus
    hold = (503, {"error_code": "quota_unknown", "unjudged_action": "hold", "retryable": True}, {"Retry-After": "0"})
    httpd.ingest = [hold, (200, {"accepted": 1}, None)]
    exporter = ArgusExporter({"endpoint": endpoint, "api_key": "k", "retry_max_attempts": 3, "retry_base_delay": 0})
    exporter.export([{"event_id": "e1", "event_type": "x"}])
    exporter.shutdown()
    assert httpd.ingest_calls == 2
    assert exporter.unsent_events() == 0


def test_export_accepted_without_judgment_warns(fake_argus, caplog):
    caplog.set_level(logging.WARNING)
    httpd, endpoint = fake_argus
    httpd.ingest = [(202, {"accepted": 0, "judged": False, "unjudged_reason": "quota_unknown", "unjudged_action": "pass", "retryable": False}, None)]
    exporter = ArgusExporter({"endpoint": endpoint, "api_key": "k"})
    exporter.export([{"event_id": "e1", "event_type": "x"}])
    exporter.shutdown()
    assert "判定を省いて記録を受け付けました: quota_unknown" in caplog.text
