"""ArgusExporter の単体テスト。"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from senda_argus_hooks.exporters import create_exporter
from senda_argus_hooks.exporters.argus import ArgusExporter


def _wait_until(predicate, timeout: float = 2.0) -> None:
    """送信は daemon スレッドに切り離されるため、キャプチャ到着まで待つ。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)


class _CaptureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.captured.append(json.loads(body.decode("utf-8")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"accepted": 1}')


@pytest.fixture
def capture_server():
    httpd = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    httpd.captured = []
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd, f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_export_sends_events(capture_server):
    httpd, endpoint = capture_server
    exporter = ArgusExporter({"type": "argus", "endpoint": endpoint, "api_key": "test-key"})
    events = [
        {
            "event_id": "e1", "event_type": "llm.request", "run_id": "run-1",
            "agent_id": "agent-1", "timestamp": "2026-07-04T00:00:00Z",
            "data": {"llm": {"provider": "ollama", "model": "llama3"}},
        }
    ]
    exporter.export(events)
    _wait_until(lambda: len(httpd.captured) == 1)
    assert len(httpd.captured) == 1
    assert httpd.captured[0]["events"][0]["event_id"] == "e1"


def test_export_empty_no_request(capture_server):
    httpd, endpoint = capture_server
    exporter = ArgusExporter({"type": "argus", "endpoint": endpoint})
    exporter.export([])
    assert len(httpd.captured) == 0


def test_fixed_run_id_override(capture_server):
    httpd, endpoint = capture_server
    exporter = ArgusExporter({"type": "argus", "endpoint": endpoint, "run_id": "fixed-run"})
    events = [{"event_id": "e1", "event_type": "llm.request", "agent_id": "a1", "data": {}}]
    exporter.export(events)
    _wait_until(lambda: len(httpd.captured) >= 1)
    assert httpd.captured[0]["events"][0]["run_id"] == "fixed-run"


def test_existing_run_id_not_overwritten(capture_server):
    httpd, endpoint = capture_server
    exporter = ArgusExporter({"type": "argus", "endpoint": endpoint, "run_id": "fixed-run"})
    events = [{"event_id": "e1", "event_type": "llm.request", "run_id": "original-run", "data": {}}]
    exporter.export(events)
    _wait_until(lambda: len(httpd.captured) >= 1)
    assert httpd.captured[0]["events"][0]["run_id"] == "original-run"


def test_export_ignores_connection_error():
    exporter = ArgusExporter({"type": "argus", "endpoint": "http://localhost:19999", "timeout": 1})
    exporter.export([{"event_id": "e1", "event_type": "llm.request", "data": {}}])


def test_registry_creates_argus_exporter(capture_server):
    _, endpoint = capture_server
    exporter = create_exporter({"type": "argus", "endpoint": endpoint})
    assert isinstance(exporter, ArgusExporter)


def test_shutdown_drains_pending_in_order():
    # shutdown は積み残したバッチを FIFO 順に送り切る。
    exporter = ArgusExporter({"type": "argus", "endpoint": "http://example"})
    seen: list = []
    exporter._send = lambda payload, headers: seen.append(
        json.loads(payload)["events"][0]["event_id"]
    )
    for i in range(5):
        exporter.export([{"event_id": f"e{i}", "data": {}}])
    exporter.shutdown()
    assert seen == ["e0", "e1", "e2", "e3", "e4"]


def test_export_does_not_block_caller():
    # 送信が遅くても export は即返る。キューへ積むだけで呼び出し側を待たせない。
    exporter = ArgusExporter({"type": "argus", "endpoint": "http://example"})
    release = threading.Event()
    exporter._send = lambda payload, headers: release.wait(2.0)
    t0 = time.monotonic()
    exporter.export([{"event_id": "e1", "data": {}}])
    elapsed = time.monotonic() - t0
    release.set()
    exporter.shutdown()
    assert elapsed < 0.5


def test_export_counts_queue_drops():
    """送出キュー満杯時の drop を計数する。沈黙 drop で欠落を見失わない。"""
    import queue as _queue

    exporter = ArgusExporter({"type": "argus", "endpoint": "http://127.0.0.1:1", "api_key": "k"})
    exporter._queue = _queue.Queue(maxsize=1)
    exporter._ensure_worker = lambda: None  # ワーカーを起こさず満杯を作る
    ev = [{"event_id": "e", "event_type": "llm.request"}]
    exporter.export(ev)  # maxsize 1 を埋める
    exporter.export(ev)  # 満杯で drop
    exporter.export(ev)  # 満杯で drop
    assert exporter.dropped_events() == 2


def test_export_counts_events_not_batches():
    """バッチが drop されたとき、バッチ数でなく含まれるイベント数を計上する。"""
    import queue as _queue

    exporter = ArgusExporter({"type": "argus", "endpoint": "http://127.0.0.1:1", "api_key": "k"})
    exporter._queue = _queue.Queue(maxsize=1)
    exporter._ensure_worker = lambda: None  # ワーカーを起こさず満杯を作る
    exporter.export([{"event_id": "first"}])  # maxsize 1 を埋める
    exporter.export([{"event_id": f"e{i}"} for i in range(5)])  # 5 件のバッチを丸ごと drop
    assert exporter.dropped_events() == 5


def _serve_status(status: int, body: dict):
    import http.server
    import json as _json
    import threading as _threading

    payload = _json.dumps(body).encode()

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_send_sync_does_not_resend_when_the_organization_is_suspended():
    from senda_argus_hooks.exporters.argus import ArgusExporter

    srv = _serve_status(
        403,
        {"error": "organization is suspended", "error_code": "organization_suspended", "retryable": False},
    )
    try:
        exp = ArgusExporter({"endpoint": f"http://127.0.0.1:{srv.server_port}"})
        assert exp.send_sync([{"event_type": "x"}]) is True
    finally:
        srv.shutdown()


def test_send_sync_keeps_other_forbidden_responses_for_resend():
    from senda_argus_hooks.exporters.argus import ArgusExporter

    srv = _serve_status(403, {"error": "forbidden"})
    try:
        exp = ArgusExporter({"endpoint": f"http://127.0.0.1:{srv.server_port}"})
        assert exp.send_sync([{"event_type": "x"}]) is False
    finally:
        srv.shutdown()


def test_send_sync_resends_when_the_status_is_unreadable():
    """停止の判定を読めない 503 は送り直してよい。取得位置を進めず記録を捨てない。"""
    from senda_argus_hooks.exporters.argus import ArgusExporter

    srv = _serve_status(
        503,
        {"error": "organization status is unavailable", "error_code": "tenant_status_unavailable", "retryable": True},
    )
    try:
        exp = ArgusExporter({"endpoint": f"http://127.0.0.1:{srv.server_port}"})
        assert exp.send_sync([{"event_type": "x"}]) is False
    finally:
        srv.shutdown()


class _FlakyIngest:
    """先頭の fail 回は status を返し、その後は 200 を返す受け取り側。event_id で重複を除いて保存する。

    store_before_failing が真なら、失敗を返す要求も先に保存する。受け取り側が保存の後で落ちて
    応答だけが 503 になった場合を表す。
    """

    def __init__(self, fail: int, status: int = 503, store_before_failing: bool = False):
        import http.server
        import threading as _threading

        self.attempts: list[list[str]] = []
        self.stored: dict[str, dict] = {}
        outer = self

        class _H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                ids = [e["event_id"] for e in body["events"]]
                outer.attempts.append(ids)
                failing = len(outer.attempts) <= fail
                if not failing or store_before_failing:
                    for e in body["events"]:
                        outer.stored.setdefault(e["event_id"], e)
                code = status if failing else 200
                self.send_response(code)
                if failing:
                    self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        _threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"


def _fast_exporter(url: str, **extra):
    return ArgusExporter(
        {"endpoint": url, "timeout": 2, "retry_base_delay": 0.0, "retry_max_delay": 0.0, **extra}
    )


def test_resends_on_503_with_the_same_event_id_and_records_once():
    srv = _FlakyIngest(fail=2, store_before_failing=True)
    try:
        exp = _fast_exporter(srv.url)
        exp.export([{"event_id": "evt_a"}, {"event_id": "evt_b"}])
        exp.shutdown()
        # 3 回送り、3 回とも同じ event_id の組。受け取り側は 2 件だけを持つ。
        assert srv.attempts == [["evt_a", "evt_b"]] * 3
        assert sorted(srv.stored) == ["evt_a", "evt_b"]
        assert exp.unsent_events() == 0
    finally:
        srv.srv.shutdown()


def test_does_not_resend_on_other_server_errors():
    srv = _FlakyIngest(fail=5, status=500)
    try:
        exp = _fast_exporter(srv.url)
        exp.export([{"event_id": "evt_a"}])
        exp.shutdown()
        assert len(srv.attempts) == 1
        assert exp.unsent_events() == 0
    finally:
        srv.srv.shutdown()


def test_gives_up_after_the_limit_and_logs_the_unsent_count(caplog):
    srv = _FlakyIngest(fail=100)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        with caplog.at_level("WARNING", logger="senda_argus_hooks.exporters.argus"):
            exp.export([{"event_id": "evt_a"}, {"event_id": "evt_b"}])
            exp.shutdown()
        assert len(srv.attempts) == 3
        assert exp.unsent_events() == 2
        assert any("今回 2 件" in r.getMessage() for r in caplog.records)
    finally:
        srv.srv.shutdown()


def test_resends_after_a_connection_failure(monkeypatch):
    import urllib.error

    from senda_argus_hooks.exporters import argus as mod

    srv = _FlakyIngest(fail=0)
    real = mod.urllib.request.urlopen
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        return real(req, timeout=timeout)

    monkeypatch.setattr(mod.urllib.request, "urlopen", flaky)
    try:
        exp = _fast_exporter(srv.url)
        exp.export([{"event_id": "evt_a"}])
        exp.shutdown()
        assert calls["n"] == 2
        assert list(srv.stored) == ["evt_a"]
        assert exp.unsent_events() == 0
    finally:
        srv.srv.shutdown()


def test_a_suspended_organization_is_not_resent_by_the_queue():
    srv = _serve_status(
        403,
        {"error": "organization is suspended", "error_code": "organization_suspended", "retryable": False},
    )
    hits = {"n": 0}
    orig = srv.RequestHandlerClass.do_POST

    def counting(self):
        hits["n"] += 1
        orig(self)

    srv.RequestHandlerClass.do_POST = counting
    try:
        exp = _fast_exporter(f"http://127.0.0.1:{srv.server_port}")
        exp.export([{"event_id": "evt_a"}])
        exp.shutdown()
        assert hits["n"] == 1
        assert exp.unsent_events() == 0
    finally:
        srv.shutdown()


def test_planned_maintenance_is_retried_beyond_the_attempt_limit():
    import http.server
    import threading as _threading

    hits = {"n": 0}

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            hits["n"] += 1
            if hits["n"] <= 8:
                self.send_response(503)
                self.send_header("Retry-After", "900")
                self.send_header("x-argus-maintenance", "planned")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        exp = _fast_exporter(
            f"http://127.0.0.1:{srv.server_port}", retry_max_attempts=3, retry_maintenance_max_delay=0.0
        )
        exp.export([{"event_id": "evt_m"}])
        exp.shutdown()
        assert hits["n"] == 9
        assert exp.unsent_events() == 0
    finally:
        srv.shutdown()
