"""tool の呼び出しの引数に現れる宛先を、正規化したホスト名の並びにする。

試験の札が付いた run からの外部通信と、主体ごとの宛先の基準線の判定が、この並びを読む。判定は
並びだけを読み、引数の形を知らない。引数の形から宛先を取り出す処理はここに置く。

**導出の規則は収集 hook と同じにする。** 収集 hook は引数の本文を送らない既定の構成でも、この
並びだけを送る。受け取り側は本文が届いた経路で同じ規則を当てる。この写しは受け取り側の実装と一字一句同じ規則にし、値の一致は受け取り側の検査で固定する。規則が 2 つに分かれると、同じ
宛先が経路ごとに違う名前になり、基準線の突合が静かに崩れる。値の一致は検査で固定する。

**名前の表記の揺れを畳む。** 大文字と小文字、末尾の点、利用者の情報とポート、角括弧で包んだ
IPv6 の綴り、パーセント符号化を揃える。揃えないと、同じ宛先を綴り替えるだけで初出に見せたり、
持ち出しに使われる宛先の一覧から外したりできる。

**宛先そのもの以外は運ばない。** 経路や問い合わせには資格情報が入ることがある。送信先の Bot の
鍵は経路に入る。ホスト名だけを出す。
"""

from __future__ import annotations

import re
from typing import Any, Final
from urllib.parse import unquote, urlsplit

# 宛先として扱う綴り。スキームを持つ URL に限る。スキームの無い語をホストとして拾うと、ファイル名や
# 識別子がホストに見える。
URL_PATTERN: Final[Any] = re.compile(r"(?i)\b(?:https?|wss?|ftps?)://[^\s\"'<>`]+")

# 値をそのままホストとして扱う引数の名前。接続先を名前で受ける tool のためにある。
HOST_KEYS: Final[frozenset[str]] = frozenset({"host", "hostname", "domain"})

# 1 件の呼び出しから出すホストの数の上限。送り手は任意の数の URL を載せられる。**上限を超えたことは
# 印で返す。** 落としたまま黙ると、既知のホストを先に並べて本命の宛先を上限の外へ押し出すだけで
# 判定を避けられる。上限を上げても同じことが起きる。判定の側は印を、宛先を判別できなかったこととして
# 扱い、検知しない側へ倒さない。
MAX_HOSTS_PER_CALL: Final[int] = 32

# ホスト名の長さの上限。DNS の名前の上限と同じ。
MAX_HOST_LEN: Final[int] = 253

# 走査する文字列の数の上限。入れ子の深い引数で処理が伸びないようにする。
MAX_SCANNED_STRINGS: Final[int] = 4096

# ホスト名に使える字。英数字と点とハイフンと、IPv6 の綴りのコロン。
_HOST_CHARS: Final[Any] = re.compile(r"^[a-z0-9.\-:]+$")


def normalize_host(raw: Any) -> str | None:
    """ホスト名を正規化する。ホストとして扱えない値は None を返す。

    URL を受けたらホストの部分を取り出す。ホスト名だけを受けたら、ポートを落として同じ形にする。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if "://" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = unquote(host).strip().lower().rstrip(".")
    if not host:
        return None
    try:
        # 国際化した名前は ASCII の綴りへ揃える。揃えないと、同じ宛先が 2 つの綴りを持つ。
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    if len(host) > MAX_HOST_LEN or not _HOST_CHARS.match(host):
        return None
    if "." not in host and ":" not in host and host != "localhost":
        # 点の無い名前は、組織の中の短い名前か、ホストではない語である。
        return None
    return host


def _strings(
    value: Any, out: list[tuple[str, str]], key: str, budget: list[int]
) -> bool:
    """引数を辿り、(引数の名前, 文字列) を集める。辞書の鍵も文字列として扱う。

    上限で打ち切ったら False を返す。打ち切った先に宛先が在りうるため、呼び出し側は印を立てる。
    """
    if budget[0] <= 0:
        return False
    if isinstance(value, str):
        budget[0] -= 1
        out.append((key, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                if budget[0] <= 0:
                    return False
                budget[0] -= 1
                out.append(("", k))
            if not _strings(v, out, k if isinstance(k, str) else "", budget):
                return False
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not _strings(item, out, key, budget):
                return False
    return True


def hosts_in_text(text: Any) -> list[str]:
    """文に現れる URL のホスト名を、現れた順に重複なく返す。"""
    if not isinstance(text, str) or not text:
        return []
    out: list[str] = []
    for match in URL_PATTERN.findall(text):
        host = normalize_host(match)
        if host and host not in out:
            out.append(host)
    return out


def egress_hosts_with_overflow(arguments: Any) -> tuple[list[str], bool]:
    """引数に現れるホスト名と、上限に収まらず落とした分があるかを返す。

    走査する文字列の上限で打ち切ったときも落とした分があるとする。打ち切った先の宛先は見ていない。
    """
    pairs: list[tuple[str, str]] = []
    complete = _strings(arguments, pairs, "", [MAX_SCANNED_STRINGS])
    out: list[str] = []
    overflow = not complete
    for key, value in pairs:
        candidates = hosts_in_text(value)
        if key.lower() in HOST_KEYS and not candidates:
            host = normalize_host(value)
            candidates = [host] if host else []
        for host in candidates:
            if host in out:
                continue
            if len(out) >= MAX_HOSTS_PER_CALL:
                overflow = True
                continue
            out.append(host)
    return out, overflow


def egress_hosts(arguments: Any) -> list[str]:
    """引数に現れるホスト名を、現れた順に重複なく返す。"""
    return egress_hosts_with_overflow(arguments)[0]
