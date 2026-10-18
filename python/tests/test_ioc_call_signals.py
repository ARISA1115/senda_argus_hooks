"""MCP の呼び出しに、引数から導いた URL とハッシュと版を載せることを固定する。

引数の本文を送らない既定の構成でも送る。文脈の無い 64 桁の 16 進は値でなくコミットメントで送る。
"""

import base64

from senda_argus_hooks.core import ioc_signals as sig
from senda_argus_hooks.core.ioc_call_fields import ioc_call_fields
from senda_argus_hooks.instrumentors.mcp_python import _mcp_metadata

H = "a" * 63 + "b"


class _Server:
    server = "s"
    url = "https://s.test"


def _meta(arguments: dict) -> dict:
    return _mcp_metadata(_Server(), "call_tool", ("fetch", arguments), {})


class TestIocFieldsOnCall:
    def test_the_default_config_sends_derived_values_and_no_body(self) -> None:
        meta = _meta({"url": "hxxps://Evil[.]example/p?k=SECRET", "body": "x"})
        assert meta[sig.IOC_URLS] == ["https://evil.example/p"]
        assert meta[sig.IOC_SIGNALS_FIELD] == sig.IOC_SIGNALS_VERSION
        assert "arguments" not in meta
        assert "SECRET" not in str(meta)

    def test_a_bare_hex_is_sent_only_as_a_commitment(self) -> None:
        meta = _meta({"note": f"see {H}"})
        assert sig.IOC_HASHES not in meta
        assert meta[sig.IOC_HASH_COMMITMENTS] == [sig.commitment(H)]
        assert H not in str(meta)

    def test_a_hex_with_context_is_sent_as_a_value(self) -> None:
        meta = _meta({"sha256": H})
        assert meta[sig.IOC_HASHES] == [H]
        assert sig.IOC_HASH_COMMITMENTS not in meta

    def test_encoded_values_are_read(self) -> None:
        token = base64.b64encode(b"go to https://paste-drop.io/x now").decode()
        meta = _meta({"blob": token})
        assert meta[sig.IOC_URLS] == ["https://paste-drop.io/x"]

    def test_the_version_is_sent_even_without_a_finding(self) -> None:
        assert ioc_call_fields({"a": "plain"}) == {sig.IOC_SIGNALS_FIELD: 1}
        assert ioc_call_fields(None) == {sig.IOC_SIGNALS_FIELD: 1}

    def test_the_cap_leaves_a_truncation_mark(self) -> None:
        urls = " ".join(
            f"https://h{i}.example/p" for i in range(sig.MAX_IOC_PER_CALL + 5)
        )
        out = ioc_call_fields({"t": urls})
        assert len(out[sig.IOC_URLS]) == sig.MAX_IOC_PER_CALL
        assert out[sig.IOC_URLS_TRUNCATED] is True
