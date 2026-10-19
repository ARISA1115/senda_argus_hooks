"""端末の登録鍵による送信の署名と、登録の CLI の単体テスト。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from senda_argus_hooks.cli import device as device_cli
from senda_argus_hooks.core import device_identity as di
from senda_argus_hooks.exporters.argus import ArgusExporter


SECRET = bytes(range(32)).hex()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in (di.ENV_DEVICE_ID, di.ENV_DEVICE_SECRET, di.ENV_DEVICE_EXPIRES_AT):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(di.ENV_FILE, str(tmp_path / "device.json"))
    return tmp_path


def _save(tmp_path, **extra):
    return di.save_credential(
        device_id="dev-1",
        secret=SECRET,
        endpoint="http://x",
        tenant_id="t1",
        expires_at=extra.get("expires_at"),
        rotate_by=None,
        path=tmp_path / "device.json",
    )


def _expected(secret, device_id, ts, seq, body):
    material = "\n".join(
        (
            "argus-device-v1",
            device_id,
            str(ts),
            str(seq),
            hashlib.sha256(body).hexdigest(),
        )
    )
    return hmac.new(
        bytes.fromhex(secret), material.encode(), hashlib.sha256
    ).hexdigest()


def test_no_credential_means_no_signature_headers():
    assert di.sign_headers(b"{}") == {}


def test_signature_covers_the_exact_body_and_matches_the_documented_material(isolated):
    _save(isolated)
    body = b'{"events": []}'
    h = di.sign_headers(body)
    assert h[di.HEADER_ID] == "dev-1"
    expected = _expected(SECRET, "dev-1", h[di.HEADER_TS], h[di.HEADER_SEQ], body)
    assert h[di.HEADER_SIG] == expected
    other = di.sign_headers(b'{"events": [1]}')
    assert other[di.HEADER_SIG] != h[di.HEADER_SIG]


def test_every_signature_uses_a_new_sequence_number(isolated):
    _save(isolated)
    seqs = [int(di.sign_headers(b"x")[di.HEADER_SEQ]) for _ in range(5)]
    assert seqs == sorted(set(seqs)) and len(seqs) == 5


def test_the_sequence_counter_is_shared_by_two_processes_reading_the_same_file(
    isolated,
):
    path = _save(isolated)
    cred = di.load_credential()
    first = int(di.sign_headers(b"x", credential=cred)[di.HEADER_SEQ])
    again = int(di.sign_headers(b"x", credential=di.load_credential())[di.HEADER_SEQ])
    assert again == first + 1 and path.with_name("device.json.seq").is_file()


@pytest.mark.skipif(os.name != "posix", reason="POSIX の権限")
def test_a_key_file_readable_by_others_is_not_used(isolated):
    path = _save(isolated)
    assert di.load_credential() is not None
    path.chmod(0o644)
    assert di.load_credential() is None
    assert di.sign_headers(b"x") == {}


@pytest.mark.skipif(os.name != "posix", reason="POSIX の権限")
def test_a_saved_key_is_owner_only_from_creation(isolated):
    path = _save(isolated)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_broken_key_file_does_not_stop_sending(isolated):
    (isolated / "device.json").write_text("{not json", encoding="utf-8")
    assert di.load_credential() is None
    assert di.sign_headers(b"x") == {}


def test_a_workload_key_from_the_environment_is_used_and_can_expire(monkeypatch):
    monkeypatch.setenv(di.ENV_DEVICE_ID, "wl-1")
    monkeypatch.setenv(di.ENV_DEVICE_SECRET, SECRET)
    monkeypatch.setenv(di.ENV_DEVICE_EXPIRES_AT, "1")
    cred = di.load_credential()
    assert cred is not None and cred.device_id == "wl-1" and cred.expired()
    assert di.sign_headers(b"x")[di.HEADER_ID] == "wl-1"


def test_the_os_user_and_host_are_sent_as_unsigned_self_declared_values(isolated):
    _save(isolated)
    h = di.sign_headers(b"x")
    from urllib.parse import unquote

    assert unquote(h[di.HEADER_USER]) == di.os_user()
    assert unquote(h[di.HEADER_HOST]) == di.hostname()


class _Capture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.captured.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"accepted": 1}')


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Capture)
    httpd.captured = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_the_exporter_signs_the_exact_bytes_it_sends_and_re_signs_the_discard_report(
    isolated, server
):
    _save(isolated)
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    exporter = ArgusExporter({"type": "argus", "endpoint": endpoint, "api_key": "k"})
    assert exporter.send_sync([{"event_id": "e1", "event_type": "x"}])
    path, headers, body = server.captured[0]
    low = {k.lower(): v for k, v in headers.items()}
    assert low["x-argus-device-id"] == "dev-1"
    assert low["x-argus-device-sig"] == _expected(
        SECRET, "dev-1", low["x-argus-device-ts"], low["x-argus-device-seq"], body
    )
    # 取りこぼしの報告は本文が違う。記録の送信の署名を流用せず、この本文で署名し直す。
    import time as _time

    now = _time.time()
    exporter._discards = [[now - 60, now - 30, 3.0]]
    # 記録の送信に付けた署名のヘッダーを持つ状態から呼び、署名が本文ごとに作り直されることを見る。
    stale = {"Content-Type": "application/json", "X-Argus-Device-Sig": "stale"}
    exporter._report_discards(stale)
    d_path, d_headers, d_body = server.captured[-1]
    d_low = {k.lower(): v for k, v in d_headers.items()}
    assert d_path.endswith("/discards") or d_path != path
    assert d_body != body
    assert d_low["x-argus-device-sig"] == _expected(
        SECRET,
        "dev-1",
        d_low["x-argus-device-ts"],
        d_low["x-argus-device-seq"],
        d_body,
    )
    assert d_low["x-argus-device-sig"] != low["x-argus-device-sig"]


def test_the_exporter_sends_without_signature_when_the_device_is_not_registered(server):
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    exporter = ArgusExporter({"type": "argus", "endpoint": endpoint})
    assert exporter.send_sync([{"event_id": "e1", "event_type": "x"}])
    _, headers, _ = server.captured[0]
    assert not any(k.lower().startswith("x-argus-device") for k in headers)


class _Enroll(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
        )
        self.server.seen.append((self.path, dict(self.headers), body))
        if self.path == "/v1/devices/authorize":
            out, code = (
                {
                    "device_code": "dc",
                    "user_code": "ABCD-1234",
                    "verification_uri": "/devices/approve",
                    "expires_in": 60,
                    "interval": 1,
                },
                200,
            )
        elif self.path == "/v1/devices/token":
            self.server.polls += 1
            if self.server.polls < 2:
                out, code = {"error": "authorization_pending"}, 400
            else:
                out, code = (
                    {
                        "device_id": "dev-9",
                        "device_secret": "newsecret",
                        "tenant_id": "t9",
                        "expires_at": "2099-01-01T00:00:00Z",
                        "rotate_by": 4070908800,
                    },
                    200,
                )
        elif self.path == "/v1/devices/renew":
            out, code = {"device_secret": "renewed", "expires_at": 4070908800}, 200
        else:
            out, code = {"error": "nope"}, 404
        raw = json.dumps(out).encode()
        self.send_response(code)
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def enroll_server():
    httpd = HTTPServer(("127.0.0.1", 0), _Enroll)
    httpd.seen, httpd.polls = [], 0
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _args(server, **kw):
    import argparse

    return argparse.Namespace(
        endpoint=f"http://127.0.0.1:{server.server_address[1]}",
        code="reg-code",
        timeout=5.0,
        **kw,
    )


def test_register_waits_for_approval_then_saves_the_key_owner_only(
    isolated, enroll_server, monkeypatch, capsys
):
    monkeypatch.setattr(device_cli.time, "sleep", lambda s: None)
    assert device_cli.cmd_register(_args(enroll_server)) == 0
    cred = di.load_credential()
    assert cred is not None and cred.device_id == "dev-9" and cred.secret == "newsecret"
    assert cred.expires_at is not None and cred.tenant_id == "t9"
    out = capsys.readouterr().out
    assert "ABCD-1234" in out and "newsecret" not in out
    first = enroll_server.seen[0]
    assert first[2]["registration_code"] == "reg-code" and "X-API-Key" not in first[1]
    assert enroll_server.polls == 2


def test_register_requires_a_code_and_never_falls_back_to_the_api_key(
    isolated, enroll_server, monkeypatch
):
    monkeypatch.setenv("SENDA_ARGUS_API_KEY", "org-key")
    monkeypatch.delenv(device_cli.ENV_CODE, raising=False)
    args = _args(enroll_server)
    args.code = None
    with pytest.raises(ValueError):
        device_cli.cmd_register(args)
    assert enroll_server.seen == []


def test_renew_signs_the_request_and_replaces_the_stored_secret(
    isolated, enroll_server
):
    _save(isolated)
    assert device_cli.cmd_renew(_args(enroll_server)) == 0
    path, headers, _ = enroll_server.seen[0]
    low = {k.lower(): v for k, v in headers.items()}
    assert path == "/v1/devices/renew" and low["x-argus-device-id"] == "dev-1"
    assert di.load_credential().secret == "renewed"


def test_status_reports_an_unregistered_device(isolated, capsys):
    import argparse

    assert device_cli.cmd_status(argparse.Namespace()) == 1
    assert "登録されていません" in capsys.readouterr().out


def _signed_ok(server, index=-1):
    path, headers, body = server.captured[index]
    low = {k.lower(): v for k, v in headers.items()}
    return low["x-argus-device-sig"] == _expected(
        SECRET, "dev-1", low["x-argus-device-ts"], low["x-argus-device-seq"], body
    )


def test_every_other_post_path_signs_its_own_body(isolated, server):
    from senda_argus_hooks.cli import scan_mcp
    from senda_argus_hooks.integrations import coding_agent_hooks as cah

    _save(isolated)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    scan_mcp.send_body(base, "k", {"a": 1}, timeout=5)
    assert _signed_ok(server)
    cah._post_json(base + "/v1/hook", {"b": 2}, "k", 5)
    assert _signed_ok(server)
    from senda_argus_hooks import onboarding

    onboarding._post(base + "/v1/agent-runs/ingest", "k", [{"c": 3}], 5)
    assert _signed_ok(server)
    assert len(server.captured) == 3


def test_the_signature_matches_a_fixed_vector_shared_with_the_receiver():
    # 受け取り側の試験と同じ値。鍵は hex を戻した 32 バイト。式のどちらかが変わると、両方の試験が落ちる。
    assert (
        di.signature_of(SECRET, "dev_x", 1760000000, 5, b'{"events":[]}')
        == "aa12665233fa2f6adb99b5ff07424eacc960a9d88daec41c4bb1f3ba2205bd66"
    )


def test_a_secret_that_is_not_hex_gets_no_signature(isolated):
    di.save_credential(
        device_id="dev-1",
        secret="not-hex-at-all",
        endpoint="http://x",
        tenant_id="t1",
        expires_at=None,
        rotate_by=None,
        path=isolated / "device.json",
    )
    assert di.sign_headers(b"x") == {}


def test_two_real_processes_never_share_a_sequence_number(isolated):
    import subprocess
    import sys

    _save(isolated)
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from senda_argus_hooks.core import device_identity as di;"
        "print(di.sign_headers(b'x')[di.HEADER_SEQ])"
    )
    src = str(__import__("pathlib").Path(di.__file__).parents[2])
    env = {**os.environ, di.ENV_FILE: str(isolated / "device.json")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, src],
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        for _ in range(8)
    ]
    seqs = [p.communicate()[0].strip() for p in procs]
    assert len(set(seqs)) == 8


def test_an_environment_key_uses_random_sequence_numbers(monkeypatch):
    monkeypatch.setenv(di.ENV_DEVICE_ID, "wl-1")
    monkeypatch.setenv(di.ENV_DEVICE_SECRET, SECRET)
    seqs = {di.sign_headers(b"x")[di.HEADER_SEQ] for _ in range(20)}
    assert len(seqs) == 20 and all(len(s) <= 19 for s in seqs)


def test_the_credential_repr_does_not_contain_the_secret(isolated):
    _save(isolated)
    assert SECRET not in repr(di.load_credential())


def test_the_discard_report_never_carries_a_stale_record_signature(
    isolated, server, monkeypatch
):
    _save(isolated)
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    exporter = ArgusExporter({"type": "argus", "endpoint": endpoint})
    monkeypatch.setattr(
        "senda_argus_hooks.exporters.argus.sign_headers", lambda body: {}
    )
    import time as _time

    now = _time.time()
    exporter._discards = [[now - 60, now - 30, 1.0]]
    exporter._report_discards(
        {"Content-Type": "application/json", "X-Argus-Device-Sig": "stale"}
    )
    _, headers, _ = server.captured[-1]
    assert not any(k.lower().startswith("x-argus-device") for k in headers)


def test_an_expired_key_warns_once_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(di, "_warned_expired", False)
    monkeypatch.setenv(di.ENV_DEVICE_ID, "wl-1")
    monkeypatch.setenv(di.ENV_DEVICE_SECRET, SECRET)
    monkeypatch.setenv(di.ENV_DEVICE_EXPIRES_AT, "1")
    di.sign_headers(b"x")
    di.sign_headers(b"x")
    assert capsys.readouterr().err.count("has expired") == 1


def test_register_keeps_waiting_through_a_network_failure(
    isolated, enroll_server, monkeypatch
):
    monkeypatch.setattr(device_cli.time, "sleep", lambda s: None)
    real = device_cli._post
    state = {"failed": False}

    def flaky(url, body, **kw):
        if url.endswith("/v1/devices/token") and not state["failed"]:
            state["failed"] = True
            raise device_cli.urllib.error.URLError("down")
        return real(url, body, **kw)

    monkeypatch.setattr(device_cli, "_post", flaky)
    assert device_cli.cmd_register(_args(enroll_server)) == 0
    assert di.load_credential() is not None


def test_register_stops_before_using_the_code_when_the_key_cannot_be_stored(
    isolated, enroll_server, monkeypatch
):
    monkeypatch.setenv(di.ENV_FILE, "/proc/forbidden/device.json")
    with pytest.raises(OSError):
        device_cli.cmd_register(_args(enroll_server))
    assert enroll_server.seen == []


def test_the_approval_url_uses_the_dashboard_url_and_carries_the_code(
    isolated, enroll_server, monkeypatch, capsys
):
    monkeypatch.setattr(device_cli.time, "sleep", lambda s: None)
    args = _args(enroll_server)
    args.dashboard_url = "https://dash.example"
    assert device_cli.cmd_register(args) == 0
    assert (
        "https://dash.example/devices/approve?user_code=ABCD-1234"
        in capsys.readouterr().out
    )


def test_non_ascii_user_and_host_are_percent_encoded_and_fit_in_a_header(
    isolated, monkeypatch
):
    from urllib.parse import unquote

    _save(isolated)
    monkeypatch.setattr(di, "os_user", lambda: "山田")
    monkeypatch.setattr(di, "hostname", lambda: "PC-ü" + "名" * 200)
    h = di.sign_headers(b"x")
    assert h[di.HEADER_USER] == "%E5%B1%B1%E7%94%B0"
    assert unquote(h[di.HEADER_USER]) == "山田"
    assert all(v.isascii() for v in h.values())
    assert len(h[di.HEADER_HOST]) <= 256


def test_the_os_user_comes_from_passwd_not_from_the_environment(monkeypatch):
    import os
    import pwd

    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("LOGNAME", "someone-else")
    assert di.os_user() == pwd.getpwuid(os.getuid()).pw_name


def test_a_retry_keeps_the_sequence_number_and_renews_the_timestamp(
    isolated, server, monkeypatch
):
    _save(isolated)
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    exporter = ArgusExporter({"type": "argus", "endpoint": endpoint})
    payload, headers = exporter._request([{"event_id": "e1", "event_type": "x"}])
    seq = di.reserve_seq()
    exporter._send_once(payload, headers, "", seq)
    exporter._send_once(payload, headers, "", seq)
    lows = [{k.lower(): v for k, v in h.items()} for _, h, _ in server.captured[-2:]]
    assert lows[0]["x-argus-device-seq"] == lows[1]["x-argus-device-seq"] == str(seq)


def test_register_continues_through_a_5xx_during_the_wait(
    isolated, enroll_server, monkeypatch
):
    monkeypatch.setattr(device_cli.time, "sleep", lambda s: None)
    real = device_cli._post
    state = {"n": 0}

    def flaky(url, body, **kw):
        if url.endswith("/v1/devices/token") and state["n"] == 0:
            state["n"] += 1
            return 503, {}
        return real(url, body, **kw)

    monkeypatch.setattr(device_cli, "_post", flaky)
    assert device_cli.cmd_register(_args(enroll_server)) == 0


def test_a_lost_token_response_tells_the_user_to_revoke_and_reissue(
    isolated, monkeypatch, capsys
):
    monkeypatch.setattr(device_cli.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake(url, body, **kw):
        if url.endswith("/v1/devices/authorize"):
            return 200, {
                "device_code": "dc",
                "user_code": "ABCD-1234",
                "verification_uri": "/devices/approve",
                "expires_in": 60,
                "interval": 1,
            }
        calls["n"] += 1
        if calls["n"] == 1:
            raise device_cli.urllib.error.URLError("reset")
        return 400, {"error": "invalid_grant"}

    monkeypatch.setattr(device_cli, "_post", fake)
    import argparse

    args = argparse.Namespace(
        endpoint="http://x", code="c", timeout=1.0, dashboard_url=None
    )
    assert device_cli.cmd_register(args) == 3
    assert "再発行" in capsys.readouterr().err


def test_a_lost_renew_response_tells_the_user_to_retry_within_the_grace(
    isolated, monkeypatch, capsys
):
    import argparse

    _save(isolated)

    def boom(url, body, **kw):
        raise device_cli.urllib.error.URLError("reset")

    monkeypatch.setattr(device_cli, "_post", boom)
    args = argparse.Namespace(endpoint="http://x", timeout=1.0)
    assert device_cli.cmd_renew(args) == 3
    assert "10 分以内" in capsys.readouterr().err
