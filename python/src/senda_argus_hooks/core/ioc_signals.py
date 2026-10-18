"""tool の呼び出しの引数と応答から、IOC の URL とハッシュを、秘匿の前の文字列で導く。

観測は、秘匿を通った要約を読むと、64 桁の 16 進が `[TOKEN]` になりハッシュを拾えない。収集 hook は
引数の本文を送らない既定の構成でも、導いた値だけを送る。この関数は受け取り側と収集 hook の両方に
同じ内容で置き、導出の値の一致を試験で固定する。規則が 2 つに分かれると、同じ呼び出しが経路ごとに
違う値になる。

**64 桁の 16 進は、ハッシュか鍵か、文字列から区別できない。** そのまま残すと、鍵を権威記録へ保存する
恐れがある。残す条件を次の 2 つに限る。

- 文脈がある。引数の鍵名が sha256、hash、digest、checksum、fingerprint を含むか、文字列の直前が
  そのような表記である
- 文脈が無い値は、組織が登録した IOC と同じ値のときだけ残す。収集 hook は組織の登録を知らないため、
  値でなくコミットメントを送り、受け取り側が登録済みの値から作った表と突き合わせる。コミットメントは
  一方向で、鍵は収集 hook の外へ出ない

**符号化された値を、上限の中で拾う。** 無害化の書き方、パーセント符号化、入れ子の JSON、base64、
16 進で符号化した文字列、バイトごとの区切り、空白で 2 つに分けられた 64 桁を、元の値に戻して拾う。
上限を超えたときは落とさず、打ち切りの印を立てる。落として黙ると、上限の外へ押し出して観測を避けられる。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any, Final
from urllib.parse import unquote, urlsplit

IOC_URLS: Final[str] = "ioc_urls"
IOC_HASHES: Final[str] = "ioc_hashes"
IOC_HASH_COMMITMENTS: Final[str] = "ioc_hash_commitments"
IOC_URLS_TRUNCATED: Final[str] = "ioc_urls_truncated"
IOC_HASHES_TRUNCATED: Final[str] = "ioc_hashes_truncated"
IOC_HASH_COMMITMENTS_TRUNCATED: Final[str] = "ioc_hash_commitments_truncated"
# 収集 hook が送る、導出の版の数。受け取り側はエージェントごとに最後に受けた版を控える。
IOC_SIGNALS_FIELD: Final[str] = "ioc_signals"
IOC_SIGNALS_VERSION: Final[int] = 1

# この関数が導く項目。送り手が平坦に置いた同じ名前の値を落とすときに使う。
FIELDS: Final[tuple[str, ...]] = (
    IOC_URLS,
    IOC_HASHES,
    IOC_HASH_COMMITMENTS,
    IOC_URLS_TRUNCATED,
    IOC_HASHES_TRUNCATED,
    IOC_HASH_COMMITMENTS_TRUNCATED,
    IOC_SIGNALS_FIELD,
)

# 1 件の呼び出しから出す値の数の上限。超えた分は落とさず、印を立てる。
MAX_IOC_PER_CALL: Final[int] = 32
MAX_COMMITMENTS_PER_CALL: Final[int] = 64
# 走査する文字列の数の上限。入れ子の深い引数で処理が伸びないようにする。
MAX_SCANNED_STRINGS: Final[int] = 4096
# 入れ子の JSON を解析する深さの上限と、解析する文字列の長さの上限。
MAX_JSON_DEPTH: Final[int] = 2
MAX_JSON_CHARS: Final[int] = 65_536
# 復号する base64 と 16 進の候補の数の上限と、1 個の復号後の長さの上限。
MAX_DECODED_TOKENS: Final[int] = 8
MAX_DECODED_BYTES: Final[int] = 4096
# 1 つの文字列を調べる長さの上限。これを超えた分は調べず、印を立てる。
MAX_TEXT_CHARS: Final[int] = 262_144

_COMMITMENT_PREFIX: Final[bytes] = b"argus-ioc-hash-v1:"
_HEX64: Final = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
_HEX_BYTES: Final = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:\- ]){31}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])"
)
_HEX_RUN: Final = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2,63}(?![0-9A-Fa-f])")
_SPLIT_GAP: Final = re.compile(r"(?:\\[nrt]|[\s\"'+,]){1,8}")
_KEY_CONTEXT: Final = re.compile(r"(?i)(sha-?256|hash|digest|checksum|fingerprint)")
_PREFIX_CONTEXT: Final = re.compile(
    r"(?i)(sha-?256|hash|digest|checksum|fingerprint)[\W_]{0,4}$"
)
_URL: Final = re.compile(r"(?i)\bhttps?://[^\s\"'<>`\\]+")
_B64: Final = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")
_HEX_TEXT: Final = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){12,}(?![0-9A-Fa-f])")
_HXXP: Final = re.compile(r"(?i)hxxp(s?)(?:://|\[:\]//|\[://\])")
_URL_TRAILING_PUNCT: Final = ".,;:!?)]}>'\"\\"
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def commitment(hex64: str) -> str:
    """文脈の無い 64 桁の 16 進のコミットメント。小文字の 16 進から作る一方向の値。"""
    return hashlib.sha256(
        _COMMITMENT_PREFIX + hex64.lower().encode("ascii")
    ).hexdigest()


def commitment_table(hashes: Any) -> dict[str, str]:
    """登録済みのハッシュの並びから、コミットメント → 値の表を作る。64 桁の 16 進だけを入れる。"""
    table: dict[str, str] = {}
    for value in hashes:
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            table[commitment(value)] = value
    return table


def normalize_url_value(raw: str) -> str | None:
    """URL を観測と登録で共通の形にそろえる。使えない URL は None。

    http と https だけを受ける。スキームとホストは小文字にし、既定のポートを落とす。userinfo と
    クエリと断片は落とす。クエリと userinfo にはトークンなどの秘密が入りうるため、観測に残さない。
    パスは保つ。末尾のスラッシュは保ち、同じ資源として畳まない。末尾の句読点は文中の区切りとして落とす。
    """
    text = raw.strip().rstrip(_URL_TRAILING_PUNCT)
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path}"


def _restore_defang(text: str) -> str:
    out = _HXXP.sub(lambda m: f"http{m.group(1)}://", text)
    for broken, plain in (
        ("[://]", "://"),
        ("[.]", "."),
        ("(.)", "."),
        ("[:]", ":"),
        ("[dot]", "."),
    ):
        out = out.replace(broken, plain)
    return out


def _printable(raw: bytes) -> str | None:
    """復号した列が、表示できる UTF-8 の文字列なら文字列を返す。そうでなければ None。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    ok = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    return text if ok / len(text) >= 0.95 else None


def _b64_decode(token: str) -> str | None:
    padded = token + "=" * (-len(token) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        if len(raw) <= MAX_DECODED_BYTES:
            text = _printable(raw)
            if text is not None:
                return text
    return None


def _variants(text: str) -> tuple[list[str], bool]:
    """1 つの文字列を調べる文の並びにする。元の文、無害化の復元、パーセント復号、符号化の復号。

    復号した文は、無害化の復元とパーセント復号までを掛け、符号化の復号は重ねない。返すのは文の並びと、
    上限で復号を打ち切ったか。
    """
    out = [text]
    cut = False
    restored = _restore_defang(text)
    base = restored
    if restored != text:
        out.append(restored)
    if "%" in base:
        decoded = unquote(base)
        if decoded != base:
            out.append(decoded)
    budget = MAX_DECODED_TOKENS
    for m in _B64.finditer(base):
        if budget <= 0:
            cut = True
            break
        token = m.group(0)
        # 16 進だけの文字列は、鍵やハッシュの可能性が高く、base64 として復号しても読める文字列にならない。
        if re.fullmatch(r"[0-9A-Fa-f]+", token):
            continue
        budget -= 1
        plain = _b64_decode(token)
        if plain is not None:
            out.append(_restore_defang(plain))
            if "%" in plain:
                out.append(unquote(_restore_defang(plain)))
    for m in _HEX_TEXT.finditer(base):
        token = m.group(0)
        if len(token) == 64:
            continue
        if budget <= 0:
            cut = True
            break
        budget -= 1
        try:
            raw = bytes.fromhex(token)
        except ValueError:
            continue
        plain = _printable(raw) if len(raw) <= MAX_DECODED_BYTES else None
        if plain is not None and "http" in plain.lower():
            out.append(_restore_defang(plain))
    return out, cut


def _walk(
    value: Any, key: str, depth: int, budget: list[int], out: list[tuple[str, str]]
) -> bool:
    """引数を辿り、(引数の名前, 文字列) を集める。入れ子の JSON の文字列は解析して辿る。

    上限で打ち切ったら False を返す。打ち切った先に値が在りうるため、呼び出し側は印を立てる。
    """
    if budget[0] <= 0:
        return False
    if isinstance(value, str):
        budget[0] -= 1
        out.append((key, value))
        stripped = value.lstrip()
        if (
            depth < MAX_JSON_DEPTH
            and stripped[:1] in ("{", "[")
            and len(value) <= MAX_JSON_CHARS
        ):
            try:
                parsed = json.loads(value)
            except ValueError:
                return True
            return _walk(parsed, key, depth + 1, budget, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                if budget[0] <= 0:
                    return False
                budget[0] -= 1
                out.append(("", k))
            if not _walk(v, k if isinstance(k, str) else "", depth, budget, out):
                return False
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not _walk(item, key, depth, budget, out):
                return False
    return True


def _hash_candidates(text: str) -> Iterator[tuple[str, int]]:
    """文に現れる 64 桁の 16 進の候補を (小文字の値, 文の中の位置) で返す。"""
    seen: set[int] = set()
    for m in _HEX64.finditer(text):
        seen.add(m.start())
        yield m.group(0).lower(), m.start()
    for m in _HEX_BYTES.finditer(text):
        yield re.sub(r"[:\- ]", "", m.group(0)).lower(), m.start()
    runs = [(m.start(), m.end(), m.group(0)) for m in _HEX_RUN.finditer(text)]
    for (s1, e1, a), (s2, _e2, b) in zip(runs, runs[1:]):
        if len(a) + len(b) == 64 and _SPLIT_GAP.fullmatch(text[e1:s2]):
            yield (a + b).lower(), s1


def _has_context(key: str, text: str, pos: int) -> bool:
    if key and _KEY_CONTEXT.search(key):
        return True
    return bool(_PREFIX_CONTEXT.search(text[max(0, pos - 24) : pos]))


def ioc_signal_fields(
    value: Any, *, known: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """呼び出しの引数か応答から、導いた IOC の項目を返す。該当しない項目は載せない。

    known は、受け取り側が組織の登録済みのハッシュから作った、コミットメント → 値の表。受け取り側は
    これを渡し、文脈の無い 64 桁の 16 進が登録済みと一致したときだけ、値を ioc_hashes へ入れる。
    収集 hook は渡さず、文脈の無い候補を、コミットメントとして ioc_hash_commitments へ入れる。
    """
    strings: list[tuple[str, str]] = []
    complete = _walk(value, "", 0, [MAX_SCANNED_STRINGS], strings)
    urls: list[str] = []
    hashes: list[str] = []
    commitments: list[str] = []
    cut_urls = cut_hashes = cut_commit = not complete
    for key, raw in strings:
        if len(raw) > MAX_TEXT_CHARS:
            cut_urls = cut_hashes = True
            raw = raw[:MAX_TEXT_CHARS]
        variants, cut = _variants(raw)
        cut_urls = cut_urls or cut
        cut_hashes = cut_hashes or cut
        for variant in variants:
            for m in _URL.finditer(variant):
                url = normalize_url_value(m.group(0))
                if url is None or url in urls:
                    continue
                if len(urls) >= MAX_IOC_PER_CALL:
                    cut_urls = True
                    continue
                urls.append(url)
            for hex64, pos in _hash_candidates(variant):
                if _has_context(key, variant, pos):
                    if hex64 in hashes:
                        continue
                    if len(hashes) >= MAX_IOC_PER_CALL:
                        cut_hashes = True
                        continue
                    hashes.append(hex64)
                    continue
                c = commitment(hex64)
                if known is not None:
                    resolved = known.get(c)
                    if resolved is not None and resolved not in hashes:
                        if len(hashes) >= MAX_IOC_PER_CALL:
                            cut_hashes = True
                        else:
                            hashes.append(resolved)
                    continue
                if c in commitments:
                    continue
                if len(commitments) >= MAX_COMMITMENTS_PER_CALL:
                    cut_commit = True
                    continue
                commitments.append(c)
    out: dict[str, Any] = {}
    if urls:
        out[IOC_URLS] = urls
    if hashes:
        out[IOC_HASHES] = hashes
    if commitments:
        out[IOC_HASH_COMMITMENTS] = commitments
    if cut_urls:
        out[IOC_URLS_TRUNCATED] = True
    if cut_hashes:
        out[IOC_HASHES_TRUNCATED] = True
    if cut_commit:
        out[IOC_HASH_COMMITMENTS_TRUNCATED] = True
    return out


def resolve_commitments(commitments: Any, known: Mapping[str, str]) -> list[str]:
    """収集 hook が送ったコミットメントを、登録済みの値へ引く。表に無いものは捨てる。"""
    out: list[str] = []
    if not isinstance(commitments, list):
        return out
    for c in commitments[:MAX_COMMITMENTS_PER_CALL]:
        if isinstance(c, str) and re.fullmatch(r"[0-9a-f]{64}", c):
            value = known.get(c)
            if value is not None and value not in out:
                out.append(value)
    return out
