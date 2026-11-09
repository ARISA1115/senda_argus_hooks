"""利用者の依頼から宛先になる値を取り出し、ソルトを付けた HMAC だけを作る。

依頼の本文は送らない。受け取り側は同じ規則で引数から値を取り出し、同じソルトの HMAC と
照合する。規則が分かれると照合できなくなるため、一致をテストで固定する。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from typing import Any, Final
from urllib.parse import unquote

from senda_argus_hooks.core.egress_hosts import hosts_in_text

# 種類ごとの上限。全体で 1 つの上限にすると、URL を並べるだけで他の種類が押し出される。
_MAX_VALUES: Final[int] = 32
_EMAIL: Final = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+")
_IBAN: Final = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_PHONE: Final = re.compile(r"(?<![\w.])\+\d[\d -]{8,16}\d(?![\w.])")
_PATH: Final = re.compile(r"(?<![\w.])(?:~|/)[\w.-]+(?:/[\w.-]+){1,12}")
_BASE64: Final = re.compile(
    r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{16,512}={0,2}(?![A-Za-z0-9+/=])"
)


def _decoded_forms(text: str) -> list[str]:
    forms = [text]
    unquoted = unquote(text)
    if unquoted != text:
        forms.append(unquoted)
    for token in _BASE64.findall(text)[:_MAX_VALUES]:
        try:
            forms.append(base64.b64decode(token, validate=True).decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
    return forms


def extract_values(text: str) -> list[str]:
    """宛先になる値を正規化して返す。ホスト、メール、IBAN、電話番号、パス。"""
    kinds: list[list[str]] = [[], [], [], [], []]
    for form in _decoded_forms(text):
        kinds[0].extend(h.lower() for h in hosts_in_text(form))
        kinds[1].extend(m.lower() for m in _EMAIL.findall(form))
        kinds[2].extend(m.upper() for m in _IBAN.findall(form))
        kinds[3].extend(re.sub(r"[ -]", "", m) for m in _PHONE.findall(form))
        kinds[4].extend(m.rstrip("/") for m in _PATH.findall(form))
    out: list[str] = []
    for kind in kinds:
        out.extend(list(dict.fromkeys(kind))[:_MAX_VALUES])
    return list(dict.fromkeys(out))


def request_values(prompt: str) -> dict[str, Any]:
    """依頼の値の HMAC とソルト。値が 1 つも無い依頼も、依頼が届いた事実として送る。"""
    salt = secrets.token_hex(16)
    return {
        "salt": salt,
        "values": [
            hmac.new(
                salt.encode("utf-8"), v.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            for v in extract_values(prompt)
        ],
    }
