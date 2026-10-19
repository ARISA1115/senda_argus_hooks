"""設定の要約の読み方と、鍵の在りかの長さの扱い。"""

from __future__ import annotations

import json
import os

import pytest
from senda_argus_hooks.cli import device as device_cli
from senda_argus_hooks.core import device_identity as di
from senda_argus_hooks.core import hook_config_digest as hcd


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in (di.ENV_DEVICE_ID, di.ENV_DEVICE_SECRET, di.ENV_DEVICE_EXPIRES_AT):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(di.ENV_FILE, str(tmp_path / "device.json"))
    return tmp_path


def test_digest_changes_even_when_size_and_mtime_are_restored(isolated, monkeypatch):
    """内容だけを変え、大きさと変更の時刻を元に戻しても、要約は変わる。"""
    cfg = isolated / "settings.json"
    cfg.write_text('{"hooks": "aaaa"}')
    st = cfg.stat()
    monkeypatch.setenv(hcd.ENV_CONFIG_FILES, str(cfg))
    before = hcd.compute()
    cfg.write_text('{"hooks": "bbbb"}')
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert cfg.stat().st_size == st.st_size and cfg.stat().st_mtime_ns == st.st_mtime_ns
    assert hcd.compute() != before


def test_unresolvable_home_path_is_counted_as_unreadable(isolated, monkeypatch):
    """解決できない ~name は例外を上げず、読めないものとして要約に入る。"""
    monkeypatch.setenv(hcd.ENV_CONFIG_FILES, "")
    base = hcd.compute()
    monkeypatch.setenv(hcd.ENV_CONFIG_FILES, "~no-such-user-for-argus-test/settings.json")
    value = hcd.compute()
    assert len(value) == 64 and value != base


def test_canary_thread_survives_a_digest_failure(monkeypatch):
    from senda_argus_hooks import onboarding

    sent = []

    class _Exporter:
        def chain_position(self):
            return ("s", 0)

        def export(self, events):
            sent.extend(events)

    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(onboarding, "_collection_active", lambda: True)
    monkeypatch.setattr(onboarding, "_argus_exporter", lambda: _Exporter())
    monkeypatch.setattr(hcd, "compute", broken)
    canary = onboarding._Canary(secret="cs1.s", generation=1, interval_sec=60, agent_id="a")
    assert canary.beat() is True
    digest = sent[0]["data"]["hook_config_digest"]
    assert len(digest) == 64


def test_a_long_key_ref_is_not_truncated_on_load(isolated):
    """上限を超える在りかは、切り詰めて別の場所を開かず、使えない鍵として扱う。"""
    long_ref = "/" + "k" * di.MAX_KEY_REF_LEN
    (isolated / "device.json").write_text(
        json.dumps(
            {
                "device_id": "dev-1",
                "device_secret": "",
                "key_kind": "software",
                "key_ref": long_ref,
            }
        )
    )
    os.chmod(isolated / "device.json", 0o600)
    assert di.load_credential() is None


def test_saving_a_too_long_key_ref_is_refused(isolated):
    with pytest.raises(ValueError):
        di.save_credential(
            device_id="dev-1",
            secret="",
            endpoint="http://x",
            tenant_id="t",
            expires_at=None,
            rotate_by=None,
            key_kind="software",
            key_ref="/" + "k" * di.MAX_KEY_REF_LEN,
        )


def test_a_key_ref_at_the_limit_round_trips(isolated):
    ref = "/" + "k" * (di.MAX_KEY_REF_LEN - 1)
    di.save_credential(
        device_id="dev-1",
        secret="",
        endpoint="http://x",
        tenant_id="t",
        expires_at=None,
        rotate_by=None,
        key_kind="software",
        key_ref=ref,
    )
    cred = di.load_credential()
    assert cred is not None and cred.key_ref == ref


def test_register_stops_before_using_the_code_when_the_key_ref_is_too_long(
    isolated, monkeypatch
):
    class _Long:
        kind = "software"
        ref = "/" + "k" * di.MAX_KEY_REF_LEN

        def public_key_b64(self):
            return "x"

    calls = []
    monkeypatch.setattr(device_cli, "_provider", lambda: _Long())
    monkeypatch.setattr(device_cli, "_post", lambda *a, **k: calls.append(a) or (200, {}))
    import argparse

    args = argparse.Namespace(endpoint="http://127.0.0.1:1", code="reg", timeout=1.0)
    assert device_cli.cmd_register(args) == 1
    assert calls == []
