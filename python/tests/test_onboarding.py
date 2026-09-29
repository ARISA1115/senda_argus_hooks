"""接続の確認と canary の送出を固定する。"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from senda_argus_hooks import onboarding


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.captured.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        reply = self.server.reply
        self.send_response(reply[0])
        self.end_headers()
        self.wfile.write(json.dumps(reply[1]).encode("utf-8"))


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.captured = []
    httpd.reply = (200, {"accepted": 1})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _no_canary():
    yield
    onboarding.stop_canary()


@pytest.fixture
def collecting(server, monkeypatch):
    """Argus へ送る exporter を持つ bus と、有効な計装が 1 つ在る状態。"""
    import importlib

    register_module = importlib.import_module("senda_argus_hooks.register")
    from senda_argus_hooks.core import runtime
    from senda_argus_hooks.core.context import RuntimeConfig
    from senda_argus_hooks.core.queue import EventBus
    from senda_argus_hooks.exporters.argus import ArgusExporter

    exporter = ArgusExporter({"endpoint": _endpoint(server), "api_key": "k-collector"})
    previous = (runtime.get_config(), runtime.get_bus())
    runtime.configure(RuntimeConfig(), EventBus(exporters=[exporter]))
    monkeypatch.setattr(register_module, "_ACTIVE_INSTRUMENTORS", [object()])
    yield server
    runtime.configure(*previous)


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)


def _endpoint(httpd) -> str:
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def test_mac_counts_utf8_bytes_like_the_server():
    assert (
        onboarding.canary_mac(
            "cs1.fixed", agent_id="エージェント-ü", boot_id="00ff00ff", seq=3, interval_sec=60, generation=1
        )
        == "Pw-KPMi63QYD61QPVxObCMlUYZeFKoAKdkWjXptO5Qs"
    )


def test_mac_matches_the_server_vector():
    """サーバの試験に同じ入力と期待値を置く。片方だけ変えると canary が一件も通らなくなる。"""
    assert (
        onboarding.canary_mac(
            "cs1.fixed", agent_id="agent-x", boot_id="00ff00ff", seq=3, interval_sec=60, generation=1
        )
        == VECTOR
    )


VECTOR = "zl3IRfLg4UC6WcViAfI9ZCfpNmclWxIWKi1f2LY6cag"


def test_connection_check_is_posted_once_to_ingest_and_returns_the_verdict(server):
    server.reply = (200, {"accepted": 1, "connection_checks": [
        {"check_id": "chk_a", "status": "verified", "reason": "verified"}
    ]})
    result = onboarding.send_connection_check(
        "ac1.chk_a.1.n.s", endpoint=_endpoint(server), api_key="k-test", agent_id="agent-1"
    )
    assert result == {"check_id": "chk_a", "status": "verified", "reason": "verified"}
    assert len(server.captured) == 1
    path, headers, body = server.captured[0]
    assert path == "/v1/agent-runs/ingest"
    assert headers["x-api-key"] == "k-test"
    (event,) = body["events"]
    assert event["event_type"] == onboarding.CONNECTION_CHECK_EVENT
    assert event["agent_id"] == "agent-1"
    assert event["data"] == {"token": "ac1.chk_a.1.n.s"}
    assert event["run_id"]


def test_connection_check_uses_the_argus_exporter_destination(collecting):
    onboarding.send_connection_check("ac1.chk_a.1.n.s", agent_id="agent-1")
    path, headers, _ = collecting.captured[0]
    assert path == "/v1/agent-runs/ingest"
    assert headers["x-api-key"] == "k-collector"


def test_connection_check_does_not_go_through_the_event_bus(server, monkeypatch):
    """バスへ流すと JSONL に値が残り、秘匿の処理が署名した項目を書き換えうる。"""
    from senda_argus_hooks.core import runtime

    emitted: list = []
    monkeypatch.setattr(runtime.get_bus(), "emit", lambda ev: emitted.append(ev))
    onboarding.send_connection_check("ac1.chk_a.1.n.s", endpoint=_endpoint(server), agent_id="a")
    assert emitted == []


def test_connection_check_never_raises(server):
    server.reply = (500, {"error": "x"})
    assert onboarding.send_connection_check("t", endpoint=_endpoint(server), agent_id="a") == {
        "status": "error", "reason": "http_500"
    }
    assert onboarding.send_connection_check("t", endpoint="http://127.0.0.1:1", agent_id="a")["status"] == "error"
    assert onboarding.send_connection_check("", endpoint=_endpoint(server))["reason"] == "empty_token"


@pytest.mark.parametrize("secret", ["", "no-generation", "csX.value", "cs1"])
def test_canary_requires_a_secret_with_a_generation(secret, monkeypatch):
    monkeypatch.delenv("SENDA_ARGUS_CANARY_SECRET", raising=False)
    assert onboarding.start_canary(secret=secret) is False


def test_canary_sends_signed_beats_through_the_argus_exporter(collecting):
    assert onboarding.start_canary(secret="cs7.s-test", interval_sec=10, agent_id="agent-1")
    _wait(lambda: len(collecting.captured) >= 1)
    assert onboarding.start_canary(secret="cs7.s-test") is False
    canary = onboarding._canary
    assert canary is not None and canary.beat() is True
    _wait(lambda: len(collecting.captured) >= 2)
    beats = [b["events"][0] for _, _, b in collecting.captured]
    assert [b["event_type"] for b in beats] == [onboarding.CANARY_EVENT] * 2
    assert {h["x-api-key"] for _, h, _ in collecting.captured} == {"k-collector"}
    first, second = (b["data"] for b in beats)
    assert (first["seq"], second["seq"]) == (0, 1)
    assert first["boot_id"] == second["boot_id"]
    assert (first["interval_sec"], first["gen"]) == (10, 7)
    for data in (first, second):
        assert data["mac"] == onboarding.canary_mac(
            "cs7.s-test", agent_id="agent-1", boot_id=data["boot_id"], seq=data["seq"],
            interval_sec=10, generation=7,
        )


def test_no_beat_without_active_instrumentation(collecting, monkeypatch):
    """計装を外した後に生きている印だけを送ると、収集の停止が検知されない。"""
    import importlib

    register_module = importlib.import_module("senda_argus_hooks.register")

    onboarding.start_canary(secret="cs1.s", interval_sec=10, agent_id="a")
    _wait(lambda: len(collecting.captured) >= 1)
    monkeypatch.setattr(register_module, "_ACTIVE_INSTRUMENTORS", [])
    assert onboarding._canary.beat() is False


def test_no_beat_without_an_argus_exporter(server, monkeypatch):
    import importlib

    register_module = importlib.import_module("senda_argus_hooks.register")

    monkeypatch.setattr(register_module, "_ACTIVE_INSTRUMENTORS", [object()])
    onboarding.start_canary(secret="cs1.s", interval_sec=10, agent_id="a")
    assert onboarding._canary.beat() is False
    assert server.captured == []


def test_shutdown_stops_the_canary(collecting):
    import senda_argus_hooks

    onboarding.start_canary(secret="cs1.s", interval_sec=10, agent_id="a")
    assert onboarding._canary is not None
    senda_argus_hooks.shutdown()
    assert onboarding._canary is None


@pytest.mark.parametrize(("given", "sent"), [(1, 10), (99999, 3600)])
def test_canary_interval_is_clamped_to_the_server_range(given, sent):
    onboarding.start_canary(secret="cs1.s", interval_sec=given, agent_id="a")
    assert onboarding._canary is not None and onboarding._canary._interval == sent


def test_start_from_env_sends_the_check_once_per_host(collecting, monkeypatch, tmp_path):
    collecting.reply = (200, {"accepted": 1, "connection_checks": [
        {"check_id": "chk_b", "status": "verified", "reason": "verified"}
    ]})
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("SENDA_ARGUS_CONNECTION_CHECK_TOKEN", "ac1.chk_b.1.n.s")
    monkeypatch.setenv("SENDA_ARGUS_CANARY_AUTOSTART", "false")
    monkeypatch.setenv("SENDA_ARGUS_CANARY_SECRET", "cs1.s-env")
    onboarding.start_from_env()
    marker = onboarding._check_marker("ac1.chk_b.1.n.s")
    # 印は送る前に原子的に取るため、判定が書かれるまで待つ。
    _wait(lambda: marker.exists() and marker.read_text(encoding="utf-8") == "verified")
    onboarding.start_from_env()
    time.sleep(0.2)
    assert len(collecting.captured) == 1
    # 止める指定のあるプロセスでは canary を始めない。
    assert onboarding._canary is None


def test_start_from_env_starts_the_canary_with_the_screen_settings(collecting, monkeypatch):
    """画面が出す環境変数だけで canary が届くこと。開始の指定を別に求めると一件も送られない。"""
    monkeypatch.delenv("SENDA_ARGUS_CONNECTION_CHECK_TOKEN", raising=False)
    monkeypatch.delenv("SENDA_ARGUS_CANARY_AUTOSTART", raising=False)
    monkeypatch.setenv("SENDA_ARGUS_CANARY_SECRET", "cs2.s-env")
    monkeypatch.setenv("SENDA_ARGUS_CANARY_INTERVAL_SEC", "30")
    onboarding.start_from_env()
    assert onboarding._canary is not None and onboarding._canary._interval == 30
    _wait(lambda: len(collecting.captured) >= 1)
    assert collecting.captured[0][2]["events"][0]["event_type"] == onboarding.CANARY_EVENT


def test_register_again_after_shutdown_restarts_the_canary(collecting, monkeypatch):
    """shutdown で止めた canary を、収集を再開したときに始め直す。始めないと途絶えの警報が続く。"""
    import senda_argus_hooks

    monkeypatch.setenv("SENDA_ARGUS_CANARY_SECRET", "cs3.s-env")
    monkeypatch.delenv("SENDA_ARGUS_CANARY_AUTOSTART", raising=False)
    onboarding.start_canary()
    senda_argus_hooks.shutdown()
    assert onboarding._canary is None
    senda_argus_hooks.register(exporters=[{"type": "null"}], auto_instrument=False)
    assert onboarding._canary is not None


@pytest.mark.parametrize("value", ["false", " n ", "0"])
def test_register_does_not_start_the_canary_when_disabled(monkeypatch, value):
    import senda_argus_hooks

    monkeypatch.setenv("SENDA_ARGUS_CANARY_SECRET", "cs3.s-env")
    monkeypatch.setenv("SENDA_ARGUS_CANARY_AUTOSTART", value)
    senda_argus_hooks.register(exporters=[{"type": "null"}], auto_instrument=False)
    assert onboarding._canary is None


def test_canary_stops_when_the_agent_id_changes(collecting, monkeypatch):
    """鍵は識別子に束ねてある。識別子が変わった後に送ると、署名が合わないか止まった収集を生きて見せる。"""
    current = {"id": "agent-old"}
    monkeypatch.setattr(onboarding, "effective_agent_id", lambda: current["id"])
    onboarding.start_canary(secret="cs1.s", interval_sec=3600)
    _wait(lambda: len(collecting.captured) >= 1)
    assert collecting.captured[0][2]["events"][0]["agent_id"] == "agent-old"
    current["id"] = "agent-new"
    assert onboarding._canary.beat() is False
    current["id"] = "agent-old"
    assert onboarding._canary.beat() is True


def test_concurrent_start_from_env_sends_the_check_once(collecting, monkeypatch, tmp_path):
    """同時に起動したプロセスのうち、印を取った 1 つだけが送る。"""
    collecting.reply = (200, {"accepted": 1, "connection_checks": [
        {"check_id": "chk_r", "status": "verified", "reason": "verified"}
    ]})
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("SENDA_ARGUS_CONNECTION_CHECK_TOKEN", "ac1.chk_r.1.n.s")
    monkeypatch.setenv("SENDA_ARGUS_CANARY_AUTOSTART", "false")
    threads = [threading.Thread(target=onboarding.start_from_env) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    marker = onboarding._check_marker("ac1.chk_r.1.n.s")
    _wait(lambda: marker.read_text(encoding="utf-8") == "verified")
    time.sleep(0.2)
    assert len(collecting.captured) == 1


def test_a_check_without_a_verdict_releases_the_marker(collecting, monkeypatch, tmp_path):
    collecting.reply = (503, {})
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("SENDA_ARGUS_CONNECTION_CHECK_TOKEN", "ac1.chk_u.1.n.s")
    monkeypatch.setenv("SENDA_ARGUS_CANARY_AUTOSTART", "false")
    marker = onboarding._check_marker("ac1.chk_u.1.n.s")
    onboarding.start_from_env()
    _wait(lambda: len(collecting.captured) == 1 and not marker.exists())
    onboarding.start_from_env()
    _wait(lambda: len(collecting.captured) == 2)
