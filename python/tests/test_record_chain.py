"""hook の記録の通し番号と連鎖の単体テスト。"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import queue
import threading
import time

import pytest
from senda_argus_hooks.core import record_chain
from senda_argus_hooks.core.record_chain import CHAIN_FIELD, RecordChain, digest_of, seq_range
from senda_argus_hooks.exporters.argus import ArgusExporter


@pytest.fixture(autouse=True)
def _fresh_chain():
    record_chain.reset_for_tests()
    yield
    record_chain.reset_for_tests()


def _independent_digest(event: dict) -> str:
    """受け取り側が書く式。hooks の実装を使わずに、契約の文面どおりに計算する。"""
    raw = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_stamp_numbers_from_one_and_links_each_record_to_the_digest_of_the_previous():
    c = RecordChain()
    out = c.stamp([{"event_id": f"e{i}", "data": {"n": i}} for i in range(3)])
    metas = [e[CHAIN_FIELD] for e in out]
    assert [m["seq"] for m in metas] == [1, 2, 3]
    assert len({m["session"] for m in metas}) == 1 and len(metas[0]["session"]) == 32
    assert metas[0]["prev"] == ""
    assert metas[1]["prev"] == _independent_digest(out[0])
    assert metas[2]["prev"] == _independent_digest(out[1])


def test_the_digest_covers_the_whole_record_so_a_rewritten_record_breaks_the_chain():
    c = RecordChain()
    first, second = c.stamp([{"event_id": "a", "data": {"x": 1}}, {"event_id": "b"}])
    tampered = json.loads(json.dumps(first))
    tampered["data"]["x"] = 2
    assert _independent_digest(tampered) != second[CHAIN_FIELD]["prev"]


def test_the_digest_survives_the_json_round_trip_for_non_ascii_and_non_json_values():
    c = RecordChain()
    first, second = c.stamp([{"event_id": "a", "data": {"name": "利用者", "when": datetime.datetime(2026, 1, 1, 9, 0)}}, {"event_id": "b"}])
    sent = json.loads(json.dumps(first, ensure_ascii=False))
    assert second[CHAIN_FIELD]["prev"] == _independent_digest(sent)


def test_stamp_does_not_change_the_callers_record():
    original = {"event_id": "a", "data": {"x": 1}}
    RecordChain().stamp([original])
    assert CHAIN_FIELD not in original


def test_one_counter_is_shared_by_all_threads_without_gaps_or_repeats():
    c = RecordChain()
    seen: list[int] = []
    lock = threading.Lock()

    def work():
        for _ in range(50):
            seq = c.stamp([{"event_id": "e"}])[0][CHAIN_FIELD]["seq"]
            with lock:
                seen.append(seq)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(seen) == list(range(1, 401))


def test_a_forked_child_starts_its_own_session_and_numbers_from_one(monkeypatch):
    c = RecordChain()
    parent = c.stamp([{"event_id": "a"}, {"event_id": "b"}])[-1][CHAIN_FIELD]
    monkeypatch.setattr(os, "getpid", lambda: os.getppid() + 12345)
    child = c.stamp([{"event_id": "c"}])[0][CHAIN_FIELD]
    assert child["session"] != parent["session"] and child["seq"] == 1 and child["prev"] == ""


def test_position_reports_the_last_number_and_take_end_answers_once():
    c = RecordChain()
    c.stamp([{"event_id": "a"}, {"event_id": "b"}])
    session, last = c.position()
    assert last == 2 and c.take_end() == (session, 2) and c.take_end() is None


def test_seq_range_groups_by_session_and_ignores_records_without_a_chain():
    events = [
        {CHAIN_FIELD: {"session": "s1", "seq": 3}},
        {CHAIN_FIELD: {"session": "s1", "seq": 5}},
        {CHAIN_FIELD: {"session": "s2", "seq": 1}},
        {"event_id": "plain"},
        {CHAIN_FIELD: {"session": "s1", "seq": True}},
    ]
    assert sorted(seq_range(events)) == [("s1", 3, 5), ("s2", 1, 1)]


# --- 送出との結び付き ---


class _Ingest:
    def __init__(self, fail_ingest: bool = False):
        import http.server

        self.events: list[dict] = []
        self.reports: list[dict] = []
        outer = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if self.path == "/v1/hooks/discards":
                    outer.reports.append(body)
                    code = 200
                else:
                    if fail_ingest:
                        code = 503
                    else:
                        outer.events.extend(body["events"])
                        code = 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b'{"accepted": 1}')

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _exporter(url: str, **extra) -> ArgusExporter:
    return ArgusExporter({"endpoint": url, "timeout": 2, "retry_base_delay": 0.0, "retry_max_delay": 0.0, **extra})


def test_exported_records_arrive_numbered_in_capture_order_with_a_valid_chain():
    srv = _Ingest()
    try:
        exp = _exporter(srv.url)
        exp.export([{"event_id": "a"}, {"event_id": "b"}])
        exp.export([{"event_id": "c"}])
        exp.shutdown()
        data = [e for e in srv.events if e.get("event_type") != "hook_session_end"]
        assert [e[CHAIN_FIELD]["seq"] for e in data] == [1, 2, 3]
        prev = ""
        for e in data:
            assert e[CHAIN_FIELD]["prev"] == prev
            prev = _independent_digest(e)
    finally:
        srv.close()


def test_a_record_dropped_from_a_full_queue_keeps_its_number_and_is_declared_as_a_range():
    srv = _Ingest()
    try:
        exp = _exporter(srv.url)
        exp._queue = queue.Queue(maxsize=1)
        exp._ensure_worker = lambda: None
        exp.export([{"event_id": "kept"}])
        exp.export([{"event_id": "lost1"}, {"event_id": "lost2"}])
        assert exp._seq_discards[0][1:] == [2, 3]
        exp._report_discards({"Content-Type": "application/json"})
        ranges = srv.reports[0]["ranges"]
        assert [(r["from"], r["to"]) for r in ranges] == [(2, 3)] and len(ranges[0]["session"]) == 32
        assert srv.reports[0]["windows"]
    finally:
        srv.close()


def test_records_that_could_not_be_sent_are_declared_by_number_after_ingest_recovers():
    srv = _Ingest(fail_ingest=True)
    try:
        exp = _exporter(srv.url, retry_max_attempts=2)
        exp.export([{"event_id": "a"}, {"event_id": "b"}])
        exp.shutdown()
        assert exp.unsent_events() >= 2
        assert exp._seq_discards and exp._seq_discards[0][1] == 1 and exp._seq_discards[0][2] >= 2
    finally:
        srv.close()


def test_adjacent_ranges_of_one_session_merge_and_the_count_is_bounded():
    exp = _exporter("http://127.0.0.1:1")
    exp._note_seq_ranges([("s", 1, 2)])
    exp._note_seq_ranges([("s", 3, 5)])
    assert exp._seq_discards == [["s", 1, 5]]
    exp._note_seq_ranges([("s", 10, 10)])
    assert exp._seq_discards == [["s", 1, 5], ["s", 10, 10]]
    for i in range(200):
        exp._note_seq_ranges([(f"t{i}", 1, 1)])
    assert len(exp._seq_discards) <= 64


def test_a_failed_range_report_is_kept_for_the_next_success():
    exp = _exporter("http://127.0.0.1:1", timeout=1)
    exp._note_seq_ranges([("s", 4, 6)])
    exp._report_discards({"Content-Type": "application/json"})
    assert exp._seq_discards == [["s", 4, 6]]


def test_shutdown_sends_one_session_end_with_the_last_number_and_not_twice():
    srv = _Ingest()
    try:
        exp = _exporter(srv.url)
        exp.export([{"event_id": "a"}, {"event_id": "b"}])
        exp.shutdown()
        exp.shutdown()
        ends = [e for e in srv.events if e.get("event_type") == "hook_session_end"]
        assert len(ends) == 1
        assert ends[0]["data"]["last_seq"] == 2 and ends[0][CHAIN_FIELD]["seq"] == 3
    finally:
        srv.close()


def test_an_exporter_that_never_sent_a_record_sends_no_session_end():
    srv = _Ingest()
    try:
        exp = _exporter(srv.url)
        exp.shutdown()
        assert srv.events == []
    finally:
        srv.close()


def test_the_pull_collector_path_does_not_join_the_hook_chain():
    exp = _exporter("http://127.0.0.1:1")
    payload, _headers = exp._request([{"event_id": "x"}])
    assert CHAIN_FIELD not in json.loads(payload)["events"][0]
    assert record_chain.chain().position()[1] == 0


def test_the_canary_carries_the_session_and_the_last_number(monkeypatch):
    from senda_argus_hooks import onboarding

    captured: list[dict] = []

    class _Exp:
        def export(self, events):
            captured.extend(events)

    monkeypatch.setattr(onboarding, "_argus_exporter", lambda: _Exp())
    monkeypatch.setattr(onboarding, "_collection_active", lambda: True)
    record_chain.chain().stamp([{"event_id": "a"}, {"event_id": "b"}, {"event_id": "c"}])
    session, _ = record_chain.chain().position()
    canary = onboarding._Canary(secret="a" * 64, generation=1, interval_sec=30, agent_id="agent-x")
    assert canary.beat() is True
    assert captured[0]["data"]["chain"] == {"session": session, "last_seq": 3}


def test_the_clock_of_the_sender_does_not_enter_the_digest_input_of_the_chain_fields():
    c = RecordChain()
    a = c.stamp([{"event_id": "a", "timestamp": "2026-01-01T00:00:00Z"}])[0]
    assert set(a[CHAIN_FIELD]) == {"session", "seq", "prev"}
    assert digest_of(a) == _independent_digest(a)
    time.sleep(0)
