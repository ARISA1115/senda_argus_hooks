"""利用者の依頼から宛先になる値と金額の数を取り出し、ソルトを付けた HMAC だけを作る。

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
import unicodedata
from typing import Any, Final
from urllib.parse import unquote

from senda_argus_hooks.core.egress_hosts import hosts_in_text

# 種類ごとの上限。全体で 1 つの上限にすると、URL を並べるだけで他の種類が押し出される。
_MAX_VALUES: Final[int] = 32
# 境界と文字は ASCII に限る。Python の \w と \b は日本語の文字も語に含め、「1,200円」や「口座DE89…へ」の
# 値を取り出せなくなる。取り出す前に NFKC で全角の英数字と記号を半角にする。
_EMAIL: Final = re.compile(
    r"[A-Za-z0-9_.+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+"
)
_IBAN: Final = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}(?![A-Za-z0-9])"
)
_PHONE: Final = re.compile(
    r"(?<![A-Za-z0-9_.])\+[0-9][0-9 -]{8,16}[0-9](?![A-Za-z0-9_.])"
)
_PATH: Final = re.compile(r"(?<![A-Za-z0-9_.])(?:~|/)[\w.-]+(?:/[\w.-]+){1,12}")
_BASE64: Final = re.compile(
    r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{16,512}={0,2}(?![A-Za-z0-9+/=])"
)
# 金額と口座の番号。2 桁以上。桁区切りのカンマは外し、宛先の値と区別するため先頭に # を付ける。
# 依頼に書いた金額を戻り値が繰り返しただけの送金を、差し替えられた値と区別するために送る。
_NUMBER: Final = re.compile(
    r"(?<![A-Za-z0-9_.])[0-9][0-9,]*(?:\.[0-9]+)?(?![A-Za-z0-9_])"
)


def _decoded_forms(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text)
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


def extract_numbers(text: str) -> list[str]:
    """数の値を正規化して返す。上限は宛先の種類ごとの上限と同じ。"""
    out: list[str] = []
    for m in _NUMBER.findall(unicodedata.normalize("NFKC", text)):
        v = _canonical_number(m)
        if len(v.replace(".", "")) >= 2:
            out.append("#" + v)
    return list(dict.fromkeys(out))[:_MAX_VALUES]


def _canonical_number(raw: str) -> str:
    """同じ値の綴りを 1 つにする。桁区切りのカンマと、小数の末尾のゼロと小数点を落とす。

    依頼の 1,200 と、ツールの引数の 1200.0 を同じ値として照合させる。口座の番号の先頭のゼロは
    意味を持つため落とさない。
    """
    v = raw.replace(",", "")
    if "." in v:
        v = v.rstrip("0").rstrip(".")
    return v


def request_values(prompt: str) -> dict[str, Any]:
    """依頼の値の HMAC とソルト。値が 1 つも無い依頼も、依頼が届いた事実として送る。"""
    salt = secrets.token_hex(16)
    return {
        "salt": salt,
        "values": [
            hmac.new(
                salt.encode("utf-8"), v.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            for v in dict.fromkeys([*extract_values(prompt), *extract_numbers(prompt)])
        ],
    }
