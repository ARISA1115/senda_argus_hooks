"""依頼の値の取り出しと HMAC を固定する。受け取り側との一致は受け取り側の試験が固定する。"""

from __future__ import annotations

import hashlib
import hmac

from senda_argus_hooks.core.request_values import extract_numbers, request_values


def _digest(salt: str, value: str) -> str:
    return hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()


def test_numbers_are_normalized() -> None:
    assert extract_numbers("pay 1,200.50 to 9876543210") == ["#1200.50", "#9876543210"]
    # 1 桁の数と、語の一部の数は取り出さない。
    assert extract_numbers("item 7 and abc123") == []


def test_a_requested_amount_is_sent_without_the_text() -> None:
    built = request_values("pay 1,200 to the vendor")
    assert _digest(built["salt"], "#1200") in built["values"]
    assert all("1200" not in v for v in built["values"])


def test_numbers_are_bounded() -> None:
    text = " ".join(str(10 + i) for i in range(100))
    assert len(extract_numbers(text)) == 32
