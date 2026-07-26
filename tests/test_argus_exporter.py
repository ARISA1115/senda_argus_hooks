"""ArgusExporter の単体テスト。"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from senda_argus_hooks.exporters.argus import ArgusExporter
from senda_argus_hooks.exporters import create_exporter


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
