"""端末の非対称の鍵の登録と署名と、canary に載せる hook の設定の要約。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from senda_argus_hooks.cli import device as device_cli
from senda_argus_hooks.core import device_identity as di
from senda_argus_hooks.core import device_keys as dk
from senda_argus_hooks.core import hook_config_digest as hcd

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in (di.ENV_DEVICE_ID, di.ENV_DEVICE_SECRET, di.ENV_DEVICE_EXPIRES_AT, dk.ENV_HELPER):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(di.ENV_FILE, str(tmp_path / "device.json"))
    hcd.reset_for_tests()
    return tmp_path


class _Server(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - 試験の出力を汚さない
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append((self.path, dict(self.headers), body))
        if self.path == "/v1/devices/authorize":
            out, code = {"device_code": "adc_x_1", "user_code": "ABCD-1234", "interval": 1, "expires_in": 60}, 200
        elif self.path == "/v1/devices/token":
            out, code = {"device_id": "dev-pk", "tenant_id": "t1", "expires_at": 4070908800,
                         "rotate_by": 4070908800, "key_protection": "software"}, 200
        else:
            out, code = {"error": "nope"}, 404
        raw = json.dumps(out).encode()
        self.send_response(code)
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Server)
    httpd.seen = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _args(server):
    import argparse

    return argparse.Namespace(
        endpoint=f"http://127.0.0.1:{server.server_address[1]}", code="reg-code", timeout=5.0
    )


def test_register_sends_a_public_key_and_saves_no_secret(isolated, server, monkeypatch):
    monkeypatch.setattr(device_cli.time, "sleep", lambda s: None)
    assert device_cli.cmd_register(_args(server)) == 0
    authorize = server.seen[0][2]
    key = serialization.load_der_public_key(base64.b64decode(authorize["public_key"]))
    assert isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1)
    cred = di.load_credential()
    assert cred is not None and cred.key_kind == dk.KIND_SOFTWARE and cred.secret == ""
    saved = json.loads((isolated / "device.json").read_text())
    assert "device_secret" in saved and saved["device_secret"] == ""
    key_file = Path(cred.key_ref)
    if os.name == "posix":
        assert not key_file.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_signature_is_ecdsa_over_the_same_material_and_verifies(isolated, server, monkeypatch):
    monkeypatch.setattr(device_cli.time, "sleep", lambda s: None)
    device_cli.cmd_register(_args(server))
    public = serialization.load_der_public_key(base64.b64decode(server.seen[0][2]["public_key"]))
    body = b'{"events":[]}'
    headers = di.sign_headers(body)
    sig = base64.urlsafe_b64decode(headers[di.HEADER_SIG] + "=" * (-len(headers[di.HEADER_SIG]) % 4))
    material = di.signing_material(
        headers[di.HEADER_ID], int(headers[di.HEADER_TS]), int(headers[di.HEADER_SEQ]), body
    )
    public.verify(sig, material.encode("utf-8"), ec.ECDSA(hashes.SHA256()))


def test_another_key_cannot_produce_the_device_signature(isolated, server, monkeypatch):
    """別の鍵の署名は、登録した公開鍵では検証できない。"""
    monkeypatch.setattr(device_cli.time, "sleep", lambda s: None)
    device_cli.cmd_register(_args(server))
    public = serialization.load_der_public_key(base64.b64decode(server.seen[0][2]["public_key"]))
    other = ec.generate_private_key(ec.SECP256R1())
    forged = other.sign(b"x", ec.ECDSA(hashes.SHA256()))
    with pytest.raises(Exception):
        public.verify(forged, b"x", ec.ECDSA(hashes.SHA256()))


def _helper(tmp_path: Path, key: ec.EllipticCurvePrivateKey, attestation: dict) -> str:
    pem = tmp_path / "helper-key.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    log = tmp_path / "helper.log"
    script = tmp_path / "helper.py"
    script.write_text(
        "import base64, json, sys\n"
        "from cryptography.hazmat.primitives import hashes, serialization\n"
        "from cryptography.hazmat.primitives.asymmetric import ec\n"
        f"key = serialization.load_pem_private_key(open({str(pem)!r},'rb').read(), None)\n"
        "cmd = sys.argv[1]\n"
        f"open({str(log)!r},'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if cmd == 'public-key':\n"
        "    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)\n"
        "    print(base64.b64encode(der).decode())\n"
        "elif cmd == 'sign':\n"
        "    print(base64.b64encode(key.sign(sys.stdin.buffer.read(), ec.ECDSA(hashes.SHA256()))).decode())\n"
        "elif cmd == 'attest':\n"
        f"    print(json.dumps(dict({attestation!r}, challenge=sys.argv[2])))\n"
    )
    wrapper = tmp_path / "helper"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(0o700)
    return str(wrapper)


@pytest.mark.skipif(os.name != "posix", reason="補助のコマンドを sh で作る")
def test_helper_key_sends_the_attestation_bound_to_the_device_code(isolated, server, monkeypatch):
    monkeypatch.setattr(device_cli.time, "sleep", lambda s: None)
    key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setenv(dk.ENV_HELPER, _helper(isolated, key, {"format": "tpm2"}))
    assert device_cli.cmd_register(_args(server)) == 0
    token_body = next(b for p, _, b in server.seen if p == "/v1/devices/token")
    expected = hashlib.sha256(b"argus-device-attest-v1\nadc_x_1").hexdigest()
    assert token_body["attestation"] == {"format": "tpm2", "challenge": expected}
    cred = di.load_credential()
    assert cred.key_kind == dk.KIND_HELPER
    headers = di.sign_headers(b"{}")
    assert headers[di.HEADER_ID] == "dev-pk"


def test_a_server_that_returns_a_secret_keeps_the_shared_secret_flow(isolated, monkeypatch):
    """公開鍵を受けない古いサーバーは秘密を返す。そのときは共有の秘密の鍵として保存する。"""
    token = {"device_id": "dev-1", "device_secret": "ab" * 32, "tenant_id": "t"}
    device_cli._store("http://x", token)
    cred = di.load_credential()
    assert cred.key_kind == "hmac" and cred.secret == "ab" * 32


def test_the_digest_changes_when_a_hook_config_file_changes(isolated, monkeypatch):
    cfg = isolated / "settings.json"
    cfg.write_text('{"hooks": {}}')
    monkeypatch.setenv(hcd.ENV_CONFIG_FILES, str(cfg))
    first = hcd.compute()
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)
    cfg.write_text('{"hooks": {"PreToolUse": []}}')
    os.utime(cfg, ns=(1, 1))
    assert hcd.compute() != first


def test_truncation_is_part_of_the_digest(isolated, monkeypatch):
    """上限で打ち切った並びは、打ち切らない並びと違う要約になる。詰め物で本物を押し出しても一致しない。"""
    monkeypatch.setenv(hcd.ENV_CONFIG_FILES, "")
    full = hcd.compute()
    hcd.reset_for_tests()
    count = len(list(hcd._package_root().rglob("*.py")))
    monkeypatch.setattr(hcd, "MAX_FILES", count - 1)
    assert hcd.compute() != full


def test_an_unreadable_config_file_changes_the_digest(isolated, monkeypatch):
    monkeypatch.setenv(hcd.ENV_CONFIG_FILES, "")
    base = hcd.compute()
    monkeypatch.setenv(hcd.ENV_CONFIG_FILES, str(isolated / "missing.json"))
    assert hcd.compute() != base


def test_the_canary_carries_the_digest(monkeypatch):
    from senda_argus_hooks import onboarding

    sent = []

    class _Exporter:
        def chain_position(self):
            return ("s", 0)

        def export(self, events):
            sent.extend(events)

    monkeypatch.setattr(onboarding, "_collection_active", lambda: True)
    monkeypatch.setattr(onboarding, "_argus_exporter", lambda: _Exporter())
    canary = onboarding._Canary(secret="cs1.s", generation=1, interval_sec=60, agent_id="a")
    assert canary.beat() is True
    assert sent[0]["data"]["hook_config_digest"] == hcd.compute()
