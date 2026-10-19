"""scan-endpoint のコマンドのテスト。偽の設定と偽の送信先を使い、秘密を送らないことと署名を付けることを見る。"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from senda_argus_hooks.cli import scan_endpoint
from senda_argus_hooks.cli.main import main

# 秘密の値は分割して組み立て、ファイルに秘密の形の文字列を直書きしない。
ENV_SECRET = "env-" + "value-" + "do-not-send-4411"
ARG_SECRET = "arg-" + "value-" + "do-not-send-5522"
HEADER_SECRET = "hdr-" + "value-" + "do-not-send-6633"
URL_PASS = "pw" + "DoNotSend7744"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    # 実行の環境の PATH にある CLI を拾わない。
    monkeypatch.setattr(scan_endpoint.shutil, "which", lambda _name: None)
    return work


def _write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _gemini(home: Path, servers: dict) -> Path:
    return _write(home / ".gemini" / "settings.json", {"mcpServers": servers})


def _report(home: Path, **kw) -> dict:
    body, _ = scan_endpoint.build_report(
        platform="linux", home=home, system_root=home / "sysroot", **kw
    )
    return body


def test_the_report_has_the_type_and_version_the_receiver_requires(tmp_path) -> None:
    body = _report(tmp_path)
    assert body["report_type"] == "endpoint_scan"
    assert body["scanner_version"] == scan_endpoint.SCANNER_VERSION


def test_the_report_sends_names_command_names_and_hosts_only(tmp_path) -> None:
    _gemini(
        tmp_path,
        {
            "local": {
                "command": f"TOKEN={ENV_SECRET} /usr/local/bin/npx",
                "args": ["--token", ARG_SECRET],
                "env": {"API_KEY": ENV_SECRET},
            },
            "remote": {
                "httpUrl": f"https://user:{URL_PASS}@api.example.com:8443/v1?key={ARG_SECRET}",
                "headers": {"Authorization": "Bearer " + HEADER_SECRET},
            },
        },
    )
    body = _report(tmp_path)
    text = json.dumps(body)
    for secret in (ENV_SECRET, ARG_SECRET, HEADER_SECRET, URL_PASS):
        assert secret not in text
    cfg = next(c for c in body["mcp_configs"] if c["client"] == "gemini_cli")
    servers = {s["name"]: s for s in cfg["servers"]}
    assert servers["local"] == {"name": "local", "command": "npx"}
    assert servers["remote"] == {
        "name": "remote",
        "command": "",
        "host": "api.example.com",
    }
    assert set(cfg) <= {"client", "location", "servers", "truncated"}
    # 設定のファイルのパスを送らない。
    assert str(tmp_path) not in text


def test_an_agent_is_reported_when_its_cli_or_settings_exist(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        scan_endpoint.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )
    _gemini(tmp_path, {"x": {"command": "npx"}})
    body, _ = scan_endpoint.build_report(
        platform="linux",
        home=tmp_path,
        system_root=tmp_path / "sysroot",
        which=scan_endpoint.shutil.which,
    )
    agents = {a["name"]: a for a in body["agents"]}
    assert agents["claude_code"]["form"] == "cli"
    assert agents["gemini_cli"]["form"] == "app"
    assert "codex" not in agents


def test_the_hook_installed_flag_reads_our_hook_entry(tmp_path) -> None:
    _write(
        tmp_path / ".claude" / "settings.json",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python -m senda_argus_hooks.cli.main hook claude_code pre",
                            }
                        ]
                    }
                ]
            }
        },
    )
    agents = {a["name"]: a for a in _report(tmp_path)["agents"]}
    assert agents["claude_code"]["hook_installed"] is True


def test_servers_over_the_limit_mark_the_config_truncated(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(scan_endpoint, "MAX_SERVERS_PER_CONFIG", 1)
    _gemini(tmp_path, {"a": {"command": "npx"}, "b": {"command": "uvx"}})
    cfg = _report(tmp_path)["mcp_configs"][0]
    assert len(cfg["servers"]) == 1 and cfg["truncated"] is True


class _Receiver(http.server.BaseHTTPRequestHandler):
    received: list[tuple[str, dict, bytes]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _Receiver.received.append(
            (self.path, dict(self.headers), self.rfile.read(length))
        )
        payload = b'{"verified": true, "accepted": 0}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        return


def test_send_posts_to_the_endpoint_scan_route_with_the_signature(
    tmp_path, monkeypatch, capsys
) -> None:
    _Receiver.received = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    signed = {"X-Argus-Device-Id": "dev_x", "X-Argus-Device-Sig": "ab"}
    seen: list[bytes] = []

    def fake_sign(data: bytes) -> dict[str, str]:
        seen.append(data)
        return dict(signed)

    monkeypatch.setattr(scan_endpoint, "sign_headers", fake_sign)
    monkeypatch.setenv("SENDA_ARGUS_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("SENDA_ARGUS_API_KEY", "k")
    try:
        code = main(
            [
                "scan-endpoint",
                "--send",
                "--home",
                str(tmp_path),
                "--system-root",
                str(tmp_path / "sysroot"),
                "--platform",
                "linux",
            ]
        )
    finally:
        server.shutdown()
    assert code == 0
    path, headers, raw = _Receiver.received[0]
    assert path == "/v1/endpoint-scans"
    assert headers.get("X-Argus-Device-Id") == "dev_x"
    # 署名したバイト列と送ったバイト列が同じ。
    assert seen == [raw]
    assert json.loads(raw)["report_type"] == "endpoint_scan"
    assert json.loads(capsys.readouterr().out)["response_status"] == 200


def test_dry_run_sends_nothing(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        scan_endpoint, "send_report", lambda *a, **k: pytest.fail("sent in dry run")
    )
    monkeypatch.setenv("SENDA_ARGUS_ENDPOINT", "http://127.0.0.1:9")
    code = main(
        [
            "scan-endpoint",
            "--send",
            "--dry-run",
            "--home",
            str(tmp_path),
            "--system-root",
            str(tmp_path / "sysroot"),
            "--platform",
            "linux",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["sent"] is False


def test_an_unkeyed_device_still_names_its_host_and_user(monkeypatch) -> None:
    # 鍵が無いと sign_headers は空を返す。ホスト名が無い報告は受け取り側が 400 で断る。
    monkeypatch.setattr(scan_endpoint, "sign_headers", lambda _data: {})
    monkeypatch.setattr(scan_endpoint, "hostname", lambda: "demo-host")
    monkeypatch.setattr(scan_endpoint, "os_user", lambda: "alice")
    headers = scan_endpoint.identity_headers(b"{}")
    assert headers == {
        "X-Argus-Device-Host": "demo-host",
        "X-Argus-Device-User": "alice",
    }


def test_the_signed_headers_are_kept_as_they_are(monkeypatch) -> None:
    signed = {
        "X-Argus-Device-Id": "dev_x",
        "X-Argus-Device-Host": "signed-host",
        "X-Argus-Device-User": "signed-user",
    }
    monkeypatch.setattr(scan_endpoint, "sign_headers", lambda _data: dict(signed))
    monkeypatch.setattr(scan_endpoint, "hostname", lambda: "other")
    assert scan_endpoint.identity_headers(b"{}") == signed
