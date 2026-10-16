"""ArgusExporter の単体テスト。"""
from __future__ import annotations

import http.client
import json
import datetime
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


def test_export_ignores_connection_error(caplog):
    exporter = ArgusExporter(
        {
            "type": "argus",
            "endpoint": "http://localhost:19999",
            "timeout": 1,
            "retry_max_attempts": 2,
            "retry_base_delay": 0,
        }
    )
    exporter.export([{"event_id": "e1", "event_type": "llm.request", "data": {}}])
    # 送出を終えてから確認する。残すと、後の試験の最中に破棄の警告が出る。
    exporter.shutdown()
    assert exporter.unsent_events() == 1
    assert "送り直しても届かなかった" in caplog.text


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


def test_retry_after_http_date_is_honored_up_to_the_cap():
    import email.utils as _eu

    exp = ArgusExporter({"endpoint": "http://127.0.0.1:9", "api_key": "k"})
    try:
        future = _eu.formatdate(time.time() + 600, usegmt=True)
        delay = exp._retry_delay(1, future, maintenance=True)
        assert 590 <= delay <= 600
        far = _eu.formatdate(time.time() + 7200, usegmt=True)
        assert exp._retry_delay(1, far, maintenance=True) == exp._maintenance_max_delay
        past = _eu.formatdate(time.time() - 60, usegmt=True)
        assert exp._retry_delay(1, past, maintenance=True) == 0.0
        assert exp._retry_delay(1, "not a date") == exp._retry_base_delay
        assert exp._retry_delay(1, "120", maintenance=True) == 120.0
    finally:
        exp.shutdown()


class _DiscardIngest:
    """先頭の fail 回の取り込みを 503 で返し、その後は 200 を返す。捨てた時間帯の報告を控える。"""

    def __init__(
        self,
        fail: int,
        discard_status: int = 200,
        ingest_status: int = 200,
        fail_status: int = 503,
        ingest_body: dict | None = None,
        truncated_reports: bool = False,
        bad_status_ingest: bool = False,
    ):
        import http.server
        import threading as _threading

        self.ingests = 0
        self.reports: list[dict] = []
        outer = self

        class _H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if self.path == "/v1/hooks/discards":
                    outer.reports.append(body)
                    if truncated_reports:
                        # 分割転送の途中で切れた応答。本文を読むと IncompleteRead になる。
                        self.send_response(200)
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        self.wfile.write(b"10\r\nab")
                        self.close_connection = True
                        return
                    code = discard_status
                else:
                    outer.ingests += 1
                    if bad_status_ingest:
                        # 状態行が壊れた応答。urlopen 自体が BadStatusLine を出す。
                        self.wfile.write(b"NOT HTTP\r\n\r\n")
                        self.close_connection = True
                        return
                    code = fail_status if outer.ingests <= fail else ingest_status
                payload = b""
                if self.path != "/v1/hooks/discards" and code == ingest_status and ingest_body:
                    payload = json.dumps(ingest_body).encode("utf-8")
                self.send_response(code)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        _threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"


# 送る前に今より先と 30 日より前を切り詰めるので、今から数える。
_BASE = float(int(time.time()) - 3600)


def _at(sec: int) -> str:
    return datetime.datetime.fromtimestamp(_BASE + sec, tz=datetime.timezone.utc).isoformat()


def test_discarded_records_are_reported_after_the_next_success():
    srv = _DiscardIngest(fail=3)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp.export([{"event_id": "a", "timestamp": _at(0)}, {"event_id": "b", "timestamp": _at(30)}])
        exp.export([{"event_id": "c", "timestamp": _at(90)}])
        exp.shutdown()
        assert exp.unsent_events() == 2
        assert srv.reports == [{"windows": [{"from": _BASE, "to": _BASE + 30, "count": 2}]}]
    finally:
        srv.srv.shutdown()


def test_a_report_that_does_not_arrive_is_kept_for_the_next_success():
    srv = _DiscardIngest(fail=3, discard_status=503)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp.export([{"event_id": "a", "timestamp": _at(0)}])
        exp.export([{"event_id": "b", "timestamp": _at(500)}])
        exp.export([{"event_id": "c", "timestamp": _at(600)}])
        exp.shutdown()
        # 報告が 2 回とも 503 でも、時間帯は捨てずに持っている。
        assert len(srv.reports) == 2
        assert srv.reports[0] == srv.reports[1]
        assert exp._discards == [[_BASE, _BASE, 1.0]]
    finally:
        srv.srv.shutdown()


def test_discard_windows_are_bounded():
    exp = ArgusExporter({"endpoint": "http://127.0.0.1:9"})
    for i in range(100):
        exp._note_discard([1_800_000_000.0 + i * 1000], 1)
    assert len(exp._discards) == 32
    assert sum(w[2] for w in exp._discards) == 100


def test_a_rejected_report_is_not_sent_again():
    srv = _DiscardIngest(fail=3, discard_status=400)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp.export([{"event_id": "a", "timestamp": _at(0)}])
        exp.export([{"event_id": "b", "timestamp": _at(500)}])
        exp.export([{"event_id": "c", "timestamp": _at(600)}])
        exp.shutdown()
        assert len(srv.reports) == 1
        assert exp._discards == []
    finally:
        srv.srv.shutdown()


def test_discards_are_not_reported_after_a_rejected_ingest():
    # 鍵の更新中の 401 は再送しないが、取り込みが回復したわけではない。報告を送ると同じ 4xx で
    # 拒まれ、持っている時間帯が戻されずに消える。
    srv = _DiscardIngest(fail=3, ingest_status=401)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp.export([{"event_id": "a", "timestamp": _at(0)}])
        exp.export([{"event_id": "b", "timestamp": _at(500)}])
        exp.shutdown()
        # 拒まれた記録も届かなかった記録として数え、報告は取り込みが 2xx で受けられるまで送らない。
        assert srv.reports == []
        assert exp._discards == [[_BASE, _BASE, 1.0], [_BASE + 500, _BASE + 500, 1.0]]
    finally:
        srv.srv.shutdown()


def test_a_truncated_report_response_does_not_stop_the_worker():
    # 本文が途中で切れた 2xx で例外を逃がすと、報告が受けられているのに時間帯が戻され、同じ報告が重ねて
    # 送られる。応答の本文は任意の情報で、2xx なら報告は受けられているので、時間帯は戻さない。
    srv = _DiscardIngest(fail=3, truncated_reports=True)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp.export([{"event_id": "a", "timestamp": _at(0)}])
        exp.export([{"event_id": "b", "timestamp": _at(500)}])
        exp.export([{"event_id": "c", "timestamp": _at(600)}])
        exp.shutdown()
        assert srv.ingests == 5
        assert len(srv.reports) == 1
        assert exp._discards == []
    finally:
        srv.srv.shutdown()


def test_a_broken_status_line_is_retried_then_counted_as_discarded_without_raising():
    srv = _DiscardIngest(fail=0, bad_status_ingest=True)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp.export([{"event_id": "a", "timestamp": _at(0)}])
        exp.export([{"event_id": "b", "timestamp": _at(500)}])
        exp.shutdown()
        # 2 件とも 3 回ずつ試し、例外でワーカーが止まらず、どちらも時間帯に数える。
        assert srv.ingests == 6
        assert len(exp._discards) == 2
    finally:
        srv.srv.shutdown()


def test_a_broken_status_line_does_not_raise_from_send_sync():
    srv = _DiscardIngest(fail=0, bad_status_ingest=True)
    try:
        exp = _fast_exporter(srv.url)
        assert exp.send_sync([{"event_id": "a", "timestamp": _at(0)}]) is False
    finally:
        srv.srv.shutdown()


def test_an_unreadable_202_body_warns_that_the_judgment_cannot_be_confirmed(caplog):
    class _Response:
        status = 202

        def read(self, n):
            raise http.client.IncompleteRead(b"ab", 10)

    exp = ArgusExporter({"endpoint": "http://127.0.0.1:9"})
    with caplog.at_level("WARNING"):
        assert exp._read_accepted(_Response(), 202) == b""
    assert "判定を省いたかを確認できません" in caplog.text


def test_a_429_is_retried_with_the_same_payload_and_not_counted_as_discarded():
    srv = _DiscardIngest(fail=2, fail_status=429)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp.export([{"event_id": "a", "timestamp": _at(0)}])
        exp.shutdown()
        assert srv.ingests == 3
        assert exp._discards == []
        assert exp.unsent_events() == 0
    finally:
        srv.srv.shutdown()


def test_a_rejected_payload_is_reported_once_ingest_recovers():
    srv = _DiscardIngest(fail=2, fail_status=400)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp.export([{"event_id": "a", "timestamp": _at(0)}, {"event_id": "b", "timestamp": _at(10)}])
        exp.export([{"event_id": "c", "timestamp": _at(20)}])
        exp.export([{"event_id": "d", "timestamp": _at(900)}])
        exp.shutdown()
        # 先の 2 回は 400 で拒まれたので、2 つの時間帯が 1 つにまとまり、3 回目の成功の後に 1 回だけ報告される。
        assert srv.reports == [{"windows": [{"from": _BASE, "to": _BASE + 20, "count": 3}]}]
        assert exp._discards == []
    finally:
        srv.srv.shutdown()


def test_discards_are_not_reported_after_a_rejected_sync_ingest():
    srv = _DiscardIngest(fail=0, ingest_status=401)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp._note_discard([_BASE], 1)
        exp.send_sync([{"event_id": "b", "timestamp": _at(500)}])
        assert srv.reports == []
        assert exp._discards == [[_BASE, _BASE, 1.0]]
    finally:
        srv.srv.shutdown()


def test_discards_are_not_reported_after_an_organization_rejection():
    # 組織の停止の符号は 403 で返る。再送しない拒否で、報告の宛先も同じ組織の状態で拒まれる。
    # 止める符号は本文に error_code と retryable=false を持つ。本文が無い 403 とは別の分岐を通る。
    body = {"error_code": "organization_read_only", "retryable": False}
    srv = _DiscardIngest(fail=0, ingest_status=403, ingest_body=body)
    try:
        exp = _fast_exporter(srv.url, retry_max_attempts=3)
        exp._note_discard([_BASE], 1)
        exp.export([{"event_id": "b", "timestamp": _at(500)}])
        exp.shutdown()
        assert srv.reports == []
        assert exp.unsent_events() == 0
        assert len(exp._discards) == 2
    finally:
        srv.srv.shutdown()


def test_a_distant_older_discard_keeps_its_own_window():
    exp = ArgusExporter({"endpoint": "http://127.0.0.1:9"})
    recent = 1_800_000_000.0
    old = recent - 20 * 86400
    exp._note_discard([recent], 5)
    exp._note_discard([old], 2)
    # 遠い過去の記録を、最近の時間帯へ寄せて件数を付け替えない。
    assert sorted(exp._discards) == [[old, old, 2.0], [recent, recent, 5.0]]


def test_a_window_count_over_the_receivers_limit_is_clipped_not_dropped():
    now = 1_900_000_000.0
    clipped = ArgusExporter._clip_windows([[now - 100, now - 50, 250_000.0], [now - 20, now - 10, 0.0]], now)
    assert [w[2] for w in clipped] == [100_000.0, 1.0]


def test_windows_stay_in_time_order_and_the_bound_merges_the_closest_pair():
    exp = ArgusExporter({"endpoint": "http://127.0.0.1:9"})
    base = 1_800_000_000.0
    # 32 の時間帯を 1000 秒おきに置き、あと 1 つを、最も新しい時間帯のすぐ後に足す。
    for i in range(32):
        exp._note_discard([base + i * 1000.0], 1)
    exp._note_discard([base + 31 * 1000.0 + 200.0], 1)
    starts = [w[0] for w in exp._discards]
    assert starts == sorted(starts)
    assert len(exp._discards) == 32
    # 時刻で最も近い 2 つがまとまり、遠い 2 つを寄せて件数を付け替えない。
    assert exp._discards[-1][2] == 2.0
    assert sum(w[2] for w in exp._discards) == 33


def test_an_older_discard_added_late_is_inserted_in_order():
    exp = ArgusExporter({"endpoint": "http://127.0.0.1:9"})
    exp._note_discard([1_800_100_000.0], 1)
    exp._note_discard([1_800_000_000.0], 1)
    exp._note_discard([1_800_050_000.0], 1)
    assert [w[0] for w in exp._discards] == [1_800_000_000.0, 1_800_050_000.0, 1_800_100_000.0]


def test_an_older_discard_within_the_margin_is_merged():
    exp = ArgusExporter({"endpoint": "http://127.0.0.1:9"})
    exp._note_discard([1_800_000_100.0], 5)
    exp._note_discard([1_800_000_070.0], 2)
    assert exp._discards == [[1_800_000_070.0, 1_800_000_100.0, 7.0]]


def test_windows_are_clipped_to_the_receivers_range():
    now = 1_900_000_000.0
    clipped = ArgusExporter._clip_windows(
        [
            [now - 40 * 86400, now - 35 * 86400, 1.0],
            [now - 20 * 86400, now - 1, 2.0],
            [now - 10, now + 600, 3.0],
        ],
        now,
    )
    assert clipped == [[now - 1 - 7 * 86400, now - 1, 2.0], [now - 10, now, 3.0]]
