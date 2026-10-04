"""永続する指示ファイルへの書き込みと、指示本文の突合可能な表現を扱う共通処理。

エージェントは文脈が切れても残る指示ファイルを持ち、その内容は次回の起動時に指示へ差し込まれる。
書き込みの権限を持つエージェントが自分の指示ファイルへ払い出しを書くと、次のエージェントが
それを指示として読む。ここが伝播の経路になる。

検知は内容を読まずに行う。払い出しが何を意図しているかの評価は意味論を要し、判定が非決定的に
なる。代わりに、書かれた本文と、後から指示に現れた本文が同じであることだけを見る。同じかどうかは
ダイジェストの一致で決まり、内容そのものは送らない。

本文全体のダイジェスト 1 つでは足りない。指示ファイルの内容は、次の起動時に他の文言と連結されて
1 つの指示になることが多く、全体のダイジェストは一致しない。そこで正規化した行ごとのダイジェスト
も併せて出す。連結されても行は保たれるため、集合の重なりとして現れる。

分類はこの層で行い、判定は受け取り側で行う。受け取り側は名前を自前の一覧と突き合わせて分類を
やり直すため、送り手の申告した真偽値をそのまま信じない。
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import posixpath
import re
import struct
import threading
import time
import unicodedata
from collections.abc import Mapping
from typing import Any, Final

# 文脈が切れても残り、次回の指示に差し込まれるファイルの名前。基底名だけで判定する。置き場所は
# 実装ごとに異なるが、名前は共通しているため。利用者の設定で足せるようにする。
INSTRUCTION_FILE_NAMES: Final[frozenset[str]] = frozenset({
    "SOUL.md",
    "MEMORY.md",
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
    ".clinerules",
    ".windsurfrules",
    ".github/copilot-instructions.md",
})

# 本文とみなす引数の名前。提供元ごとに異なる。
#
# 書き込みかどうかをツール名から判定しない。名前は提供元ごとに自由で、部分一致にすると
# create_issue や get_updates のような無関係な名前まで拾い、逆に apply_diff のような名前は
# 取りこぼす。判別に効いているのは「指示ファイルを指すパス」と「本文にあたる引数」が揃うことで、
# 読み取り系のツールは本文を引数に取らないため、この 2 条件で十分に絞れる。名前による絞り込みを
# 足しても誤検知は減らず、取りこぼしだけが増える。
_BODY_KEYS: Final[tuple[str, ...]] = (
    "content", "text", "body", "data", "new_string", "new_str", "contents",
    "value", "file_text", "source", "patch", "diff",
)

# 差分として渡される引数の名前。**差分かどうかは引数の名前で決める。** 本文の綴りで判定すると、
# ファイルの中身に位置情報の行や封筒の見出しが書かれているだけで差分として扱ってしまう。差分と
# 判定した本文は先頭が削除の記号である行を捨てるため、書き手はその形を本文へ書くだけで、実際には
# 保存される行を控えから消せる。名前で決めれば、この経路は塞がる。
_PATCH_BODY_KEYS: Final[frozenset[str]] = frozenset({"patch", "diff"})

# パスとみなす引数の名前。
_PATH_KEYS: Final[tuple[str, ...]] = (
    "path", "file_path", "filename", "file", "target_path", "uri", "filepath",
)

# 1 件あたりに出す行ダイジェストの上限。指示ファイルは大きくなりうるため、送出量と受け取り側の
# 保持量に上限を置く。上限を超えた分は落とす。落ちた行が突合から漏れるだけで、誤検知にはならない。
# 指示にあたる役割の名前。この役割の本文が変わることは、指示が変わることを意味する。
SYSTEM_ROLE: Final[str] = "system"

# 指示として扱う役割。**提供元ごとに名前が違う。** 応答系の要求は、優先して従わせる指示を
# developer の役割で受ける。system だけを見ると、その形の指示が 1 件も拾えない。
INSTRUCTION_ROLES: Final[frozenset[str]] = frozenset({SYSTEM_ROLE, "developer"})

# 実測で、3 つの計画の文書 209 件のうち、64 では 133 件しか全体を運べない。256 なら 202 件が
# 収まる。上限いっぱいでも、1 件 71 バイトのダイジェストが 256 件で 18,176 バイトに収まる。上限を超える本文では、一部だけを見た
# 書き込みと全体を見た指示とで残る組が食い違い、突合が成立しないことがある。
MAX_LINE_DIGESTS: Final[int] = 256

# 突合の対象にする行の最小の長さ。短い行は無関係な文書どうしでも一致するため、集合の重なりが
# 伝播の証拠にならなくなる。
MIN_LINE_LENGTH: Final[int] = 24

_DIGEST_PREFIX: Final[str] = "sha256:"

# 語の組を作るときに、突合の対象とする語の最小の長さ。**組に使う語は位置を指すものに限る**ため、
# 普通の語を除くための長さは短くてよい。長さで絞ると etc/shadow や ssh/id_rsa のような短い経路が
# 落ち、払い出しがそのまま取りこぼしになる。
MIN_TOKEN_LENGTH: Final[int] = 10

# 同じ行の語を並べ替えて重複を除いた列の上で、いくつ先の語まで組にするか。**元の並びの近さは
# 使わない。** 要約は語の順序を変えるため、並びの近さで組を選ぶと、組み替えただけで一致しなく
# なる。窓を広げても分離は変わらず、組の数だけが増える。
TOKEN_PAIR_WINDOW: Final[int] = 3

# 1 件あたりに出す組ダイジェストの上限。行ごとのダイジェストと同じ考えで、送出量と受け取り側の
# 保持量に上限を置く。
MAX_PAIR_DIGESTS: Final[int] = 64

# 書き込み側の上限。**指示側と同じ上限を書き込み側へ課すと、埋め草で押し出せる。** 書き手は
# 本文を自由に決められるため、値の順で先に来る組を必要なだけ足して、払い出しの組を上限の外へ
# 追い出せる。指示側は要約された短い本文なので払い出しの組が残り、突合だけが成立しなくなる。
# 書き込みは指示ファイルへの書き込みに限られ、頻度も低い。落とさずに全部載せる。上限は
# 際限なく積み上げないための歯止めとしてだけ置く。
MAX_WRITE_DIGESTS: Final[int] = 4096

# 語とみなす文字の並び。区切りに使う記号を語の内側へ残す。残さないと、経路や URL や識別子が
# 細切れになり、要約を経ても保たれるという性質が失われる。バックスラッシュの区切りも語の内側に残す。
# 残さないと、その区切りを使う環境の経路が 1 文字ごとに切れ、組が 1 つも作れない。
#
# **クエリとフラグメントとパーセント符号化も語の内側に残す。** 切ると、指す先が問い合わせで分かれるURLが
# 同じ語に潰れる。テナントごとに宛先を分けた URL がすべて 1 つになり、無関係な指示ファイルどうしが
# 下限に届く。
# 角括弧も語の内側に残す。囲まれた形で書く宛先があり、外すとホストの部分が丸ごと落ちて、
# 経路だけが同じ別のホストが同じダイジェストになる。
# **記号を 1 つずつ足さない。** 足りない記号が見つかるたびに追加すると、次の記号でまた同じ
# 取りこぼしが出る。URL の綴りで区切りとして使える記号を規格の一覧からまとめて入れる。
# 予約された区切りと下位の区切り、および符号化されない文字である。
_TOKEN_RE: Final[Any] = re.compile(r"[A-Za-z0-9\-._~%:/?#\[\]@!$&'()*+,;=\\]+")

# 語を切る役割と、語の内側を保つ役割を分ける。**同じ記号が両方を担う。** 読点や分号は URL の
# 内側にも現れるし、宛先を並べる区切りにも使われる。文字の集合だけで決めると、内側を保てば
# 並びが 1 語に潰れ、切れば内側が失われる。どちらか一方しか選べない。
#
# 判断の根拠は記号そのものではなく、**その直後に新しい起点が始まるかどうか**である。起点の
# 定義は突合で使うものと同じで、区切りか駆動名か種別から始まる形を指す。並びを区切る記号の
# 直後に起点が来たら、そこから次の語を始める。
_LIST_SEPARATORS: Final[str] = ",;"

# 大小を無視してよい部分。**経路の大小は意味を持つ。** 語をまるごと小文字へ倒すと、
# /srv/TenantA と /srv/tenanta が同じダイジェストになり、別の対象を指す組が一致する。
# URL のホスト名とスキームだけを倒し、あわせてドライブ文字から始まる経路も倒す。前者は綴りが大小を区別せず、
# 後者はその環境の経路そのものが大小を区別しない。
_SCHEME_SEP: Final[str] = "://"
_DRIVE_RE: Final[Any] = re.compile(r"[A-Za-z]:")

# その位置から種別が始まる形。**後方のどこかに区切りがあることを起点の証拠にしない。**
_SCHEME_AT_RE: Final[Any] = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")


def _fold_case(token: str) -> str:
    """大小を無視してよい部分だけ倒す。"""
    if _DRIVE_RE.match(token):
        return token.lower()
    head, sep, rest = token.partition(_SCHEME_SEP)
    if not sep:
        return token
    # **権限の終わりはスラッシュだけではない。** クエリやフラグメントが直後に来る形もある。
    # 区切りを 1 つしか見ないと、クエリの値まで倒れて大小の違う別の宛先が同じになる。
    cut = len(rest)
    for mark in ("/", "?", "#"):
        found = rest.find(mark)
        if found != -1 and found < cut:
            cut = found
    authority, tail = rest[:cut], rest[cut:]
    # **利用者情報は大小を区別する。** 権限の部分をまるごと倒すと、@ の手前が違うだけの
    # 別の宛先が同じダイジェストになる。倒すのはホスト名だけにする。
    userinfo, at, hostname = authority.rpartition("@")
    return head.lower() + _SCHEME_SEP + userinfo + at + hostname.lower() + tail

# 組に使う語に含まれていることを求める区切り。**長さと文字種だけでは足りない。** 同じ計画の
# 文書は識別子の語彙を共有し、規則名や事象名のような下線や点を含む長い語が、無関係な文書どうしで
# 並んで現れる。実測で、3 つの計画の文書から写しを除いた 199 件を総当たりした 19701 対のうち、
# 長さと文字種だけで絞ると 183 対が 2 組以上、121 対が 3 組以上重なった。位置を指す語に限ると
# どちらも 0 対になる。
#
# 位置を指す語だけが、要約を経ても書き換えられずに残るという性質を持つ。要約は文言を作り替えるが、
# 払い出しが指す先は書き換えられない。
_TOKEN_LOCATOR: Final[str] = "/"

# 起点の定まった表記かどうかの判定。**起点の無い表記は指す先を定めない。** 文書の中の
# 相互参照は ../x や ./x や docs/x の形を取り、同じ計画の文書どうしが同じ綴りを共有する。
# 綴りが同じでも指す先は文書ごとに違うため、証拠にならない。
_ROOTED_PREFIXES: Final[tuple[str, ...]] = ("/", "~/")


def _is_rooted(token: str) -> bool:
    """起点が定まった経路か URL かを返す。"""
    return _starts_rooted(token, 0)


def _starts_rooted(text: str, at: int) -> bool:
    """その位置から起点が始まるかを返す。**位置だけを見る。**

    後ろのどこかに種別の区切りがあることを起点の証拠にしない。区切りの直後に起点が始まるかを
    問うているので、同じ語の後方に別の宛先があるだけで真を返してはならない。真を返すと、区切りの
    直後にある値がそのまま捨てられる。

    切り出しはこの判定を語の長さの回数だけ呼ぶ。**後方をすべて走査する形では長さの 2 乗になる。**
    本文は書き手が決められるため、区切りを並べるだけで導出に時間を使わせられる。位置を渡して
    その場だけを見る。
    """
    if text.startswith(_ROOTED_PREFIXES, at):
        return True
    if _DRIVE_RE.match(text, at):
        return True
    # 種別は駆動名の直後に来る。語の後方にある別の宛先の区切りを拾わないよう、位置から
    # 続く綴りが種別の形をしているかだけを見る。
    return _SCHEME_AT_RE.match(text, at) is not None



def _digest(value: str) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()


def instruction_file_name(path: Any) -> str | None:
    """パスが指示ファイルを指すなら、一覧に載っている名前を返す。

    判定は基底名で行う。ただし一覧に区切りを含む名前がある場合は、末尾の一致でも認める。
    """
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return None
    base = posixpath.basename(text)
    for known in INSTRUCTION_FILE_NAMES:
        if "/" in known:
            if text.endswith(known):
                return known
        elif base == known:
            return known
    return None


# 差分と判定する目印。**書き方は 1 つではない。** 統一形式の位置情報の行に加えて、道具が使う
# 封筒の形も見る。封筒の形は位置情報の行が裸の @@ で、統一形式の目印に当たらない。見落とすと
# 加えた行の先頭の記号が残ったまま語を作り、その行の最初の宛先が起点を持たない語として捨てられる。
_DIFF_MARKERS: Final[tuple[str, ...]] = (
    "@@ ", "--- ", "+++ ", "*** Begin Patch", "*** Update File:", "*** Add File:",
)

# 統一形式の位置情報の行。**範囲を伴うことを求める。** 裸の目印 1 つを差分の証拠として
# 受け取ると、書き手が目印を置くだけで削除の記号から始まる行を控えから消せる。
_UNIFIED_HUNK_RE: Final[Any] = re.compile(r"\A@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")

# 封筒の形が使う行。本文ではないため落とす。
_PATCH_ENVELOPE_MARKERS: Final[tuple[str, ...]] = (
    "*** Begin Patch", "*** End Patch", "*** Update File:", "*** Add File:",
    "*** Delete File:", "*** Move to:",
)


def _looks_like_patch(body: str) -> bool:
    """本文が差分形式かどうかを、封筒の構造が揃っているかで判定する。

    **目印 1 つで差分と決めない。** 差分と判定した本文は、先頭が削除の記号である行を捨てる。
    書き手は目印を 1 行置いて、続けて払い出しを削除の記号で始まる箇条書きとして書くだけで、
    書き込みの控えから証拠を丸ごと消せる。指示側は箇条書きをそのまま読むため、突合だけが
    成立しなくなる。

    統一形式の位置情報の行は範囲を伴う。この形は普通の文には現れないため、1 行でも構造の証拠に
    なる。**範囲を持たない裸の目印は証拠にしない。** 箇条書きの中に置くだけで書ける。

    封筒の形は位置情報が裸の目印になるが、始まりの行と対象ファイルの行が並ぶ。この組が揃って
    初めて差分とみなす。揃わない本文は、記号で始まる行を含んでいてもそのまま扱う。
    """
    lines = body.splitlines()
    if any(_UNIFIED_HUNK_RE.match(raw) for raw in lines):
        return True
    has_envelope_start = any(raw.startswith("*** Begin Patch") for raw in lines)
    has_envelope_target = any(
        raw.startswith(("*** Update File:", "*** Add File:", "*** Delete File:"))
        for raw in lines
    )
    return has_envelope_start and has_envelope_target


def normalize_patch_body(body: Any, *, is_patch: bool = False) -> Any:
    """差分形式の本文を、適用後に残る文言へ均す。

    **差分かどうかは呼び出し側が決める。** 引数の名前が差分を表すときだけ真を渡す。本文の綴りで
    判定すると、ファイルの中身に位置情報の行が書かれているだけで差分として扱い、実際には保存
    される行を捨てる。既定は偽で、そのまま返す。

    書き込みが差分で渡された場合、行の先頭に付く記号を落とさずにダイジェストへ通すと、後から
    指示に現れる同じ行と一致しない。指示側には記号の付かない行が載るためである。差分でない
    本文はそのまま返す。

    削除の行は適用後に残らないため落とす。位置情報の行も本文ではないため落とす。

    **文脈の行も落とす。** 差分の文脈は、この書き込みが加えたものではなく元から在った文言である。
    残すと、位置を指す語が 3 つ並ぶ行の隣を書き換えただけで、その行をこの主体が書いたことに
    なる。後から別の主体がその行を含む指示を読むと、無関係な書き換えを根拠に伝播として報告
    される。差分で渡された書き込みの証拠は、加えた行だけから作る。
    """
    if not isinstance(body, str) or not body or not is_patch or not _looks_like_patch(body):
        return body
    kept: list[str] = []
    for raw in body.splitlines():
        if raw.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        if raw.startswith(_PATCH_ENVELOPE_MARKERS):
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+"):
            kept.append(raw[1:])
            continue
        if raw.startswith(" "):
            continue
        kept.append(raw)
    return "\n".join(kept)


class BoundedDigestSet:
    """走査しながら、値の小さい順に上限までを保つ。

    **上限を超える分を最後にまとめて落とす形では、走査中の確保が上限に縛られない。** 大きな
    本文では、出すのが数百件でも数十万件を抱えて並べ替えることになる。上限の数倍まで溜めたら
    その場で切り、以後は切った境目より大きい値を持たない。落とすのは最終的に残らない値だけ
    なので、結果は最後にまとめて選ぶ場合と同じになる。
    """

    __slots__ = ("_ceiling", "_limit", "_overflowed", "_seen", "_slack")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._slack = max(limit * 4, limit + 1)
        self._seen: set[str] = set()
        self._ceiling: str | None = None
        # **落としたことを黙って忘れない。** 落とした事実まで消すと、上限を超える本文を書けば
        # 証拠が静かに欠けたまま完全な記録に見える。落としたかどうかは読み手が知る必要がある。
        self._overflowed = False

    def add(self, digest: str) -> None:
        if self._ceiling is not None and digest > self._ceiling:
            self._overflowed = True
            return
        self._seen.add(digest)
        if len(self._seen) > self._slack:
            self._prune()

    def _prune(self) -> None:
        if len(self._seen) > self._limit:
            self._overflowed = True
        kept = sorted(self._seen)[: self._limit]
        self._seen = set(kept)
        self._ceiling = kept[-1] if kept else None

    def overflowed(self) -> bool:
        """上限に収まらず落とした分があるかを返す。"""
        return self._overflowed or len(self._seen) > self._limit

    def result(self) -> list[str]:
        return capped_digests(self._seen, self._limit)


def capped_digests(digests: set[str], limit: int) -> list[str]:
    """上限を超える分を、本文の位置に依らない決まった順で落とす。

    **文頭から詰めて打ち切ると、末尾に書かれたものが必ず落ちる。** 指示ファイルは既存の内容へ
    追記して育つため、後から書かれた払い出しがちょうど落ちる位置に来る。攻撃側は本文の長さを
    伸ばすだけで、控えにも指示側にも自分の書いたものを載せずに済む。

    ダイジェストの値の順で選ぶ。値は内容から決まり位置に依らないため、どこに書いたかで残るか
    どうかを選べない。書き込み側と指示側で同じ選び方を通すので、突合はそのまま成立する。
    """
    ordered = sorted(digests)
    return ordered[:limit] if len(ordered) > limit else ordered


def line_digests(
    body: Any, *, limit: int = MAX_LINE_DIGESTS, is_patch: bool = False
) -> list[str]:
    """本文を正規化した行ごとのダイジェストにする。

    前後の空白を落として空行を除く。短い行は無関係な文書どうしでも一致するため除く。同じ行が
    繰り返されても 1 つに畳む。出現順は保たず、集合として扱う。差分形式の本文は、適用後に残る
    文言へ均してから通す。
    """
    return line_digests_with_overflow(body, limit=limit, is_patch=is_patch)[0]


def line_digests_with_overflow(
    body: Any, *, limit: int = MAX_LINE_DIGESTS, is_patch: bool = False
) -> tuple[list[str], bool]:
    """行ごとのダイジェストと、上限に収まらず落とした分があるかを返す。"""
    body = normalize_patch_body(body, is_patch=is_patch)
    if not isinstance(body, str) or not body:
        return [], False
    seen = BoundedDigestSet(limit)
    for raw in body.splitlines():
        line = raw.strip()
        if len(line) < MIN_LINE_LENGTH:
            continue
        seen.add(_digest(line))
    return seen.result(), seen.overflowed()


# 文の側から宛先を包む字。**語に使える字のうち、起点の先頭には来られないもの**である。
# 起点は区切りか駆動名か種別から始まるため、これらの字が語の先頭にあれば必ず文の側である。
#
# 括弧は開きと閉じが別の字で、宛先の内側でも対になる。引用符と強調の印は同じ字で開いて閉じ、
# 宛先の内側にも同じ字が現れうる。**包み方を 1 つずつ足さない。** 指示ファイルは Markdown で
# 書かれ、シェルの引用符や太字や斜体やリンクで宛先を包む。包み方ごとに手当てすると、次の
# 包み方でまた組が 1 つも作れなくなる。
_PROSE_WRAPPERS: Final[str] = "[]()'*_"

# 同じ字で開いて閉じる包み。先頭で開いた数だけ末尾から閉じ、内側の同じ字は残す。
_SYMMETRIC_WRAPPERS: Final[str] = "'*_"

# 閉じた包みの後ろに続く句読点。閉じる字が控えているときだけ文の側として落とす。
# 包まれていない語の末尾の点は経路の一部でありうるため、ここでは触らない。
_PROSE_PUNCTUATION: Final[str] = ".,;:!?"


def _strip_prose_wrappers(token: str) -> str:
    """語の外側を包む字と、包みの後ろに続く句読点だけを落とす。

    包みを語へ取り込むと起点の判定に落ち、組が 1 つも作れない。要約で包みが付いたり外れたり
    するだけでその語を含む組が全部変わり、要約を経ても保たれるという性質を失う。

    宛先の内側の括弧は対になっているため落ちない。内側の引用符は、先頭で開いた数を超えて
    末尾から落とさないため残る。

    **落とす量に比例した仕事で済ませる。** 1 つずつ切り出すと、そのたびに残りを複製することに
    なり、包みが続く長さの 2 乗の仕事になる。書き手は本文を自由に決められるため、包みを並べた
    本文を書くだけで導出に時間を使わせられる。位置を数えてから 1 度で切る。
    """
    head = 0
    opened = dict.fromkeys(_SYMMETRIC_WRAPPERS, 0)
    while head < len(token) and token[head] in _PROSE_WRAPPERS:
        if token[head] in opened:
            opened[token[head]] += 1
        head += 1
    token = token[head:]
    # 末尾から落としてよい数。括弧は対にならずに余っている閉じの数、同じ字の包みは先頭で開いた数。
    # 数え直しは 1 度で済ませ、末尾を削るたびに全体を数えない。
    closable = dict(opened)
    closable["]"] = token.count("]") - token.count("[")
    closable[")"] = token.count(")") - token.count("(")
    tail = len(token)
    while tail > 0:
        if closable.get(token[tail - 1], 0) > 0:
            closable[token[tail - 1]] -= 1
            tail -= 1
            continue
        mark = tail
        while mark > 0 and token[mark - 1] in _PROSE_PUNCTUATION:
            mark -= 1
        if mark == tail or mark == 0 or closable.get(token[mark - 1], 0) <= 0:
            break
        tail = mark
    return token[:tail]


def _strip_assignment_prefix(token: str) -> str:
    """語の前に付いた代入や選択肢の名前を落とす。

    指示ファイルは経路や URL を `KEY=/srv/agent/config` や `--log=/var/log/audit.log` の形でも
    書く。前置きを残すと、経路は起点を持たない語として捨てられ、URL は前置きごとダイジェストに
    なる。要約が裸の経路だけを残すと、書き込み側と指示側で別の値になり突合が落ちる。

    落とすのは、等号の手前に区切りも種別の印も無いときだけにする。`https://host/a?tenant=A` の
    ように手前が既に位置を指している場合は、クエリの値を切り離してしまうため触らない。
    """
    # **1 度の走査で切る位置を決める。** 等号ごとに切り出して繰り返すと、そのたびに残りを
    # 複製することになり、等号が続く長さの 2 乗の仕事になる。本文は書き手が決められるため、
    # 等号を並べるだけで導出に時間を使わせられる。
    start = 0
    for i, ch in enumerate(token):
        if ch != "=":
            continue
        head = token[start:i]
        if _TOKEN_LOCATOR in head or ":" in head:
            break
        if i + 1 >= len(token):
            break
        start = i + 1
    return token[start:]


def _split_at_next_locator(token: str) -> list[str]:
    """並びを区切る記号の直後に起点が始まるなら、そこで語を分ける。

    **判断の根拠は記号ではなく、その直後に新しい起点が始まるかどうかである。** 記号で一律に
    切ると URL の内側が失われ、切らないと宛先の並びが 1 語に潰れて組が 1 つも作れない。起点の
    判定は突合で使うものと同じものを使い回す。定義を 2 つ持つと、片方だけ変えたときに切り方と
    突合が食い違う。

    **区切りの直後の包みは読み飛ばしてから問う。** 宛先を引用符や括弧で包んで並べる書き方では、
    区切りの直後は包みの字で、起点はその後ろから始まる。包みの字を見て起点ではないと判断すると、
    並びが 1 語に潰れる。Markdown のリンクは閉じ角括弧の直後に丸括弧で宛先を包むため、その
    境目も区切りとして扱う。
    """
    out: list[str] = []
    start = 0
    # この位置より手前まで、包みの字の連なりを読み終えている。区切りのたびに同じ連なりを
    # 読み直すと、区切りと包みを交互に並べた本文で長さの 2 乗の仕事になる。
    wrapped_until = 0
    for i, ch in enumerate(token):
        if i + 1 >= len(token):
            continue
        if ch not in _LIST_SEPARATORS and not (ch == "]" and token[i + 1] == "("):
            continue
        at = i + 1
        if at < wrapped_until:
            at = wrapped_until
        else:
            while at < len(token) and token[at] in _PROSE_WRAPPERS:
                at += 1
            wrapped_until = at
        if not _starts_rooted(token, at):
            continue
        piece = token[start:i]
        if piece:
            out.append(piece)
        start = i + 1
    tail = token[start:]
    if tail:
        out.append(tail)
    return out or [token]


def _distinctive_tokens(text: str) -> list[str]:
    """1 行から、位置を指す語だけを取り出す。

    経路と URL に限る。**語の一覧は持たない。** 一覧は言語ごとに要り、維持できない。区切りを
    含むことと長さだけなら、どの言語でも同じ手続きで決まる。

    取り出せるのは ASCII で書かれた経路と URL に限られる。日本語だけで書かれた指示ファイルからは
    組が出ない。その構成では行ごとの突合だけが働く。
    """
    out: list[str] = []
    for matched in _TOKEN_RE.findall(text or ""):
        for raw in _split_at_next_locator(matched):
            # バックスラッシュはスラッシュへ均す。同じ対象を指す経路が、環境の書き方の違いだけで
            # 別のダイジェストになると突合が成立しない。
            #
            # **先頭の区切りは落とさない。** 落とすと /srv/a と srv/a が同じダイジェストになり、
            # 起点の違う別の対象を指す組が一致する。落とすのは文の側の記号だけにする。
            # **末尾の点は落とさない。** 経路の一部でありうるため、落とすと /srv/a. と /srv/a が
            # 同じダイジェストになり、別の対象を指す組が一致する。落とすのは経路の末尾に来ない
            # 記号だけにする。
            # **経路の区切りは落とさない。** 末尾の斜線は宛先の一部でありうる。落とすと
            # /api と /api/ が同じダイジェストになり、別の資源を指す組が一致する。落とすのは
            # 文の側にしか現れない記号だけにする。
            token = raw.replace("\\", "/").rstrip(",;:~@?#&").lstrip("-:@")
            token = _strip_prose_wrappers(token)
            token = _strip_assignment_prefix(token)
            token = _fold_case(token)
            if len(token) < MIN_TOKEN_LENGTH:
                continue
            if _TOKEN_LOCATOR not in token:
                continue
            # **起点の無い表記は、指す先を定めない。** 文書の中の相互参照はこの形を取り、同じ
            # 計画の文書どうしが同じ綴りを共有する。指す先が同じとは限らないため証拠にならない。
            if not _is_rooted(token):
                continue
            out.append(token)
    return out


def token_pair_digests(
    body: Any, *, limit: int = MAX_PAIR_DIGESTS, is_patch: bool = False
) -> list[str]:
    """本文を、同じ行に現れた珍しい語の組ごとのダイジェストにする。

    要約を経ると行はそのまま残らないが、払い出しが指す先、すなわち経路や URL は書き換えられずに
    残る。**1 語では足りない。** 同じ計画の文書どうしは語彙を共有するため、語 1 つの一致は
    無関係な文書の間でも起きる。組にすると、組み合わせが一致する確率は大きく下がる。実測で、
    3 つの計画の文書から写しを除いた 199 件を総当たりした 19701 対のうち、3 組以上重なった対は
    無かった。

    **組は同じ行の中でだけ作る。** 連続する行をまとめると、同じ計画の文書どうしで 3 組以上の
    重なりが出る。実測で連続 2 行をまとめた場合は 1 対、本文全体では 3 対が 3 組以上重なった。

    組は並べ替えてから作り、窓も並べ替えた語の列に当てる。要約は語の順序を変えるため、順序を
    含めると突合が成立しない。

    本文そのものは返さない。内容を運ばずに突合できる形だけを出す。
    """
    return token_pair_digests_with_overflow(body, limit=limit, is_patch=is_patch)[0]


def token_pair_digests_with_overflow(
    body: Any, *, limit: int = MAX_PAIR_DIGESTS, is_patch: bool = False
) -> tuple[list[str], bool]:
    """語の組のダイジェストと、上限に収まらず落とした分があるかを返す。"""
    body = normalize_patch_body(body, is_patch=is_patch)
    if not isinstance(body, str) or not body:
        return [], False
    seen = BoundedDigestSet(limit)
    for raw in body.splitlines():
        # **窓は元の並び順でなく、並べ替えて重複を除いた語の列に当てる。** 組ごとに並べ替えても、
        # どの語どうしを組にするかを元の並びの近さで決めると、宛先の並びを組み替えただけの要約で
        # 組が 1 つも一致しなくなる。実測で、20 個の宛先を番号順に並べた行と、番号を 4 で割った
        # 余りでまとめ直した行は、どちらも 54 組を出して共通の組が 0 だった。
        tokens = sorted(set(_distinctive_tokens(raw)))
        for i in range(len(tokens)):
            for j in range(i + 1, min(i + 1 + TOKEN_PAIR_WINDOW, len(tokens))):
                # 列は並べ替えて重複を除いてあるため、左は常に右より小さく、同じ語どうしは組にならない。
                seen.add(_digest(f"{tokens[i]}\x1f{tokens[j]}"))
    return seen.result(), seen.overflowed()


def system_prompt_pair_digests(*sources: Any, **named: Any) -> list[str]:
    """指示の本文を、突合可能な語の組ごとのダイジェストにする。

    書き込み側と同じ導出を通す。両側で規則が違うと、同じ組が違うダイジェストになり突合が
    成立しない。入力の受け方は行ごとの導出と揃える。
    """
    texts: list[str] = []
    for source in list(sources) + list(named.values()):
        texts.extend(_texts_from(source))
    texts = [t for t in texts if t]
    if not texts:
        return []
    return token_pair_digests("\n".join(texts))


def _first_present_key(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """最初に見つかった名前を返す。値ではなく名前で扱いを分けるために要る。"""
    for key in keys:
        if key in source and source[key] is not None:
            return key
    return None


def _first_present(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def instruction_file_path(arguments: Any) -> str | None:
    """引数が指示ファイルを指していれば、指している場所をそのまま返す。指していなければ None。"""
    if not isinstance(arguments, dict):
        return None
    raw = _first_present(arguments, _PATH_KEYS)
    if not isinstance(raw, str) or instruction_file_name(raw) is None:
        return None
    return raw.strip()


def classify_instruction_write(arguments: Any) -> dict[str, Any] | None:
    """指示ファイルへの書き込みなら、突合に使う情報を返す。該当しなければ None。

    返すのは、一覧に載っている名前と、本文全体のダイジェストと、行ごとのダイジェストと、
    語の組ごとのダイジェストである。行は要約を経ると残らないため、組も併せて出す。
    本文そのものは返さない。内容を運ばずに突合できる形だけを出す。
    """
    if not isinstance(arguments, dict):
        return None
    name = instruction_file_name(_first_present(arguments, _PATH_KEYS))
    if name is None:
        return None
    body_key = _first_present_key(arguments, _BODY_KEYS)
    body = arguments.get(body_key) if body_key else None
    if not isinstance(body, str) or not body:
        return None
    # 差分として扱うのは、差分を表す名前で渡ったときだけにする。本文の綴りでは決めない。
    is_patch = body_key in _PATCH_BODY_KEYS
    lines, lines_over = line_digests_with_overflow(
        body, limit=MAX_WRITE_DIGESTS, is_patch=is_patch
    )
    pairs, pairs_over = token_pair_digests_with_overflow(
        body, limit=MAX_WRITE_DIGESTS, is_patch=is_patch
    )
    # 文の署名は埋め込みと鍵が揃うときだけ出る。揃わない構成では空で、欄を載せない。
    semantic, semantic_over = semantic_band_digests_with_overflow(
        body, limit=MAX_WRITE_SEMANTIC_SENTENCES, is_patch=is_patch
    )
    written: dict[str, Any] = {
        "instruction_file_name": name,
        "written_content_hash": _digest(body),
        # 書き込み側は落とさない。落とすと、書き手が埋め草で払い出しを押し出せる。
        "written_line_hashes": lines,
        "written_pair_hashes": pairs,
    }
    # **上限に収まらなかったことを記録する。** 有限の控えは無限の本文を保てないため、上限は
    # どこかに要る。落とした事実まで消すと、上限を超える本文を書くだけで証拠が静かに欠け、
    # 欠けた記録が完全な記録と見分けられなくなる。実測で正規の指示ファイルは最大 48 組で、
    # 上限はその 85 倍である。超える書き込みは指示ファイルの体を成しておらず、超過そのものが
    # 判定の材料になる。
    if semantic:
        # 語の組が作れない本文のための証拠。本文も埋め込みも載せず、鍵付きのダイジェストだけを載せる。
        written["written_semantic_hashes"] = semantic
    if lines_over or pairs_over or semantic_over:
        written["written_digests_truncated"] = True
    return written


# 指示にあたる本文が載る引数の名前。提供元ごとに異なる。役割つきの列に載る場合と、独立した
# 引数で渡る場合と、オブジェクトの属性として保持される場合がある。
_INSTRUCTION_KEYS: Final[tuple[str, ...]] = (
    "system", "system_instruction", "instructions", "systemInstruction",
)

# 役割つきの要素が載る引数の名前。応答系の要求では input に載る。
_ROLE_LIST_KEYS: Final[tuple[str, ...]] = ("messages", "input", "contents")

# 役割の宣言がある要素だけを採る引数の名前。**これらの名前には利用者の入力も載る。** 応答系の
# 要求は指示と質問を同じ引数で受け、埋め込みの要求は文書そのものを、生成の要求は利用者の
# 問いかけをここへ渡す。役割の無い値をまとめて指示として扱うと、経路やURLを含む普通の質問や
# 文書が指示のダイジェストになり、記録済みの書き込みと偶然重なったときに伝播として報告される。
#
# **役割の載る名前は全部この扱いにする。** 1 つだけ条件を付けても、同じ性質の残りの名前から
# 同じことが起きる。
_ROLE_REQUIRED_KEYS: Final[tuple[str, ...]] = _ROLE_LIST_KEYS


def _is_sequence(value: Any) -> bool:
    """列として辿ってよい値かを返す。**組み込みの列だけに限らない。**

    提供元の SDK は、繰り返しの欄を組み込みの列ではない独自の型で持つ。指示の塊の部品の列も、
    役割つきの要素の列も、その型で届く。組み込みの列だけを列として扱うと、その型で届いた指示から
    本文が 1 文字も取れず、突合が成立しない。

    文字列とバイト列と写像は列として扱わない。要素の数と添字と繰り返しを型が持つものだけを列と
    みなす。型で見るのは、属性を引かれると動的に応じる型があり、インスタンスで見ると誤るため。
    1 度しか辿れない繰り返しは数を持たないため、ここで取り込まない。
    """
    if isinstance(value, (list, tuple)):
        return True
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return False
    kind = type(value)
    return all(hasattr(kind, name) for name in ("__iter__", "__len__", "__getitem__"))


def _flatten_batch(value: Any) -> Any:
    """束ねられた列を 1 段ほどく。

    枠組みによっては、1 回の要求に複数の会話を束ねて渡す。外側の列は役割を持たないため、
    ほどかずに渡すと役割の判定も本文の取り出しも成立せず、指示が 1 件も拾えない。
    """
    if not _is_sequence(value) or not len(value):
        return value
    if all(_is_sequence(item) for item in value):
        return [inner for item in value for inner in item]
    return value


def _declares_role(value: Any) -> bool:
    """役割を宣言した要素を含む列かどうかを返す。"""
    if not _is_sequence(value):
        return False
    return any(_role_of(item) for item in value)


def _block_text(block: Any) -> str:
    """種別つきの塊から本文を取り出す。形は提供元ごとに異なる。

    **本文が部品の列に入る形もある。** 提供元によっては、指示を 1 つの塊として持ち、その
    中の部品に文字列を分けて置く。直下の文字列だけを見ると、その形の指示から本文が 1 文字も
    取れない。部品の列があれば、そこまで辿って連結する。
    """
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        for key in ("text", "content", "input_text"):
            value = block.get(key)
            if isinstance(value, str):
                return value
        return _parts_text(block.get("parts"))
    text = getattr(block, "text", None)
    if isinstance(text, str) and text:
        return text
    return _parts_text(getattr(block, "parts", None))


def _parts_text(parts: Any) -> str:
    """部品の列から本文を連結する。列でなければ空を返す。"""
    if not _is_sequence(parts):
        return ""
    texts = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
            continue
        value = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
        if isinstance(value, str) and value:
            texts.append(value)
    return "\n".join(texts)


def _role_of(item: Any) -> str:
    """要素が宣言した役割を返す。宣言が無ければ空を返す。

    役割を載せる名前は提供元ごとに違う。枠組みによっては要素の種別として持つ。片方だけを
    見ると、その枠組みの指示が 1 件も拾えない。**種別を見るのは要素が辞書でないときに限る。**
    辞書の種別は本文の塊の種類を表しており、役割ではない。
    """
    if isinstance(item, dict):
        return str(item.get("role") or "").strip().lower()
    role = getattr(item, "role", "") or getattr(item, "type", "")
    return str(role or "").strip().lower()


def _content_of(item: Any) -> Any:
    if isinstance(item, dict):
        for key in ("content", "parts", "text"):
            if key in item:
                return item[key]
        return None
    for key in ("content", "parts", "text"):
        value = getattr(item, key, None)
        if value is not None:
            return value
    return None


def _texts_from(source: Any) -> list[str]:
    """1 つの入力から、指示にあたる本文を取り出す。

    受け取る形は 3 通りある。役割つきの要素の列、種別つきの塊の列、文字列そのものである。
    列の場合は役割が指示にあたる要素だけを採る。役割を持たない塊の列は、その全体が指示として
    渡されたものとして扱う。
    """
    texts: list[str] = []
    if source is None:
        return texts
    if isinstance(source, str):
        return [source] if source else []
    if isinstance(source, dict):
        return [t for t in [_block_text(source)] if t]
    if _is_sequence(source):
        has_role = any(_role_of(item) for item in source)
        for item in source:
            if has_role:
                if _role_of(item) not in INSTRUCTION_ROLES:
                    continue
                content = _content_of(item)
            else:
                content = item
            if isinstance(content, str):
                if content:
                    texts.append(content)
            elif _is_sequence(content):
                texts.extend(t for t in (_block_text(b) for b in content) if t)
            else:
                t = _block_text(content)
                if t:
                    texts.append(t)
        return texts
    t = _block_text(source)
    return [t] if t else []


def collect_instruction_sources(
    call_kwargs: Any = None, positional: Any = None, holder: Any = None
) -> list[Any]:
    """呼び出しと保持元から、指示が載りうる箇所を集める。

    指示は 3 つの場所のいずれかにある。キーワード引数、位置引数、そして呼び出し対象の
    オブジェクトが保持する属性である。どこに載るかは提供元と操作ごとに違うため、名前の候補を
    網羅して集め、取り出し側では場所を意識しない。
    """
    sources: list[Any] = []
    if isinstance(call_kwargs, dict):
        for key in _INSTRUCTION_KEYS + _ROLE_LIST_KEYS:
            if call_kwargs.get(key) is None:
                continue
            value = call_kwargs[key]
            if key in _ROLE_REQUIRED_KEYS:
                value = _flatten_batch(value)
                if not _declares_role(value):
                    continue
            sources.append(value)
    if _is_sequence(positional):
        for item in positional:
            if _is_sequence(item):
                flat = _flatten_batch(item)
                if _declares_role(flat):
                    sources.append(flat)
    if holder is not None:
        for key in _INSTRUCTION_KEYS:
            for name in (key, f"_{key}"):
                value = getattr(holder, name, None)
                if value is not None:
                    sources.append(value)
                    break
    return sources


def system_prompt_line_digests(*sources: Any, **named: Any) -> list[str]:
    """指示の本文を、突合可能な行ごとのダイジェストにする。

    書き込み側と同じ正規化と同じ下限を通す。両側で規則が違うと、同じ行が違うダイジェストに
    なり突合が成立しない。規則をこの層に集約するのはそのためである。

    入力は形を問わない。役割つきの列、種別つきの塊、文字列、いずれも受ける。提供元ごとに
    指示の渡り方が違うため、呼び出し側は持っているものをそのまま渡せばよい。
    """
    texts: list[str] = []
    for source in list(sources) + list(named.values()):
        texts.extend(_texts_from(source))
    texts = [t for t in texts if t]
    if not texts:
        return []
    return line_digests("\n".join(texts))


# ---------------------------------------------------------------------------
# 文の署名。**語の組が作れない本文のための突合である。**
#
# 語の組は ASCII で書かれた経路と URL からしか作れず、日本語だけで書かれた指示ファイルからは 1 つも
# 作れない。要約は文言を作り替えるため行も残らない。文の意味は要約を経ても残るので、指示と書き込みを
# 文に分け、ローカルの多言語の埋め込みモデルで埋め込み、テナントごとの鍵から作った超平面で符号に
# 変え、符号を帯に分けて帯ごとの鍵付きダイジェストだけを出す。
#
# **本文も埋め込みも出さない。** 埋め込みからは元の文をある程度戻せる。出すのは鍵付きのダイジェスト
# だけで、鍵を持たない受け取り側からは符号の値も戻せない。
#
# **鍵をテナントごとにする。** 鍵が共通だと、別のテナントの署名と照合でき、同じ文を持つかどうかが
# 漏れる。鍵は超平面と帯のダイジェストの両方に掛ける。
#
# **埋め込みか鍵のどちらかが無ければ何も出さない。** 判別できないものを既定の値へ倒さない。
# ---------------------------------------------------------------------------

# テナントの鍵を読む環境変数。16 進で書いた 32 バイト以上の値に限る。記録にも送出にも載せない。
SEMANTIC_KEY_ENV: Final[str] = "SENDA_ARGUS_INSTRUCTION_SIGNATURE_KEY"

# ローカルに置いた埋め込みモデルのディレクトリ。実在するディレクトリだけを受け付け、外部へは
# 接続しない。モデルの識別子は受け付けない。識別子を受けると、置いていないときに取得へ落ちる。
SEMANTIC_MODEL_DIR_ENV: Final[str] = "SENDA_ARGUS_INSTRUCTION_EMBED_MODEL_DIR"

# 鍵の長さの下限。短い鍵は総当たりで超平面を推せる。
SEMANTIC_KEY_MIN_BYTES: Final[int] = 32

# 突合の対象にする文の最小の文字数。短い文は無関係な文書どうしでも同じ意味になる。
SEMANTIC_MIN_SENTENCE_CHARS: Final[int] = 12

# 1 文を埋め込みへ渡す前に切る長さ。埋め込みの時間は文の長さに比例して伸びる。書き手は本文を
# 自由に決められるため、長い文を並べるだけで導出に時間を使わせられる。
SEMANTIC_MAX_SENTENCE_CHARS: Final[int] = 256

# 指示側で 1 件あたりに埋め込む文の上限。指示は毎回の呼び出しに載るため、時間に効く。
MAX_SEMANTIC_SENTENCES: Final[int] = 64

# 書き込み側で 1 件あたりに埋め込む文の上限。**指示側と同じ上限を書き込み側へ課すと、埋め草で
# 押し出せる。** 書き込みは指示ファイルへの書き込みに限られ頻度が低いため、上限を広く取る。
MAX_WRITE_SEMANTIC_SENTENCES: Final[int] = 256

# 帯の数と、1 つの帯に入る符号の桁の数。桁を増やすと無関係な文が同じ帯に入りにくくなり、帯を
# 増やすと言い換えた文がどれかの帯で揃いやすくなる。
SEMANTIC_BANDS: Final[int] = 32
SEMANTIC_ROWS: Final[int] = 48

# 1 件あたりの埋め込みに掛けてよい時間。埋め込みは文の塊ごとに呼び、超えたら残りを落とす。
# **落とすのは値の順で後ろの文で、本文の位置に依らない。** 時間は送り手が決められないので、
# 落ちたことを書き込みの超過の印には数えない。
SEMANTIC_MAX_SECONDS: Final[float] = 2.0

# 埋め込みへ 1 度に渡す文の数。時間の上限はこの単位で確かめる。
SEMANTIC_BATCH: Final[int] = 16

# 帯のダイジェストの接頭辞。行と組の sha256 と形で分ける。
SEMANTIC_DIGEST_PREFIX: Final[str] = "hmac-sha256:"

# 受け付ける埋め込みの次元の範囲。外れた値は壊れた埋め込みとして何も出さない。
_SEMANTIC_MIN_DIM: Final[int] = 8
_SEMANTIC_MAX_DIM: Final[int] = 4096

# 文の区切り。改行と、文の終わりの記号の後ろで切る。半角の点は後ろに空白があるときだけ切る。
# 数字や経路の中の点で文を切らないため。
_SENTENCE_SPLIT_RE: Final[Any] = re.compile(r"\n+|(?<=[。!?])|(?<=\.)\s+")

# 文の頭に付く箇条書きと見出しの印。要約で付いたり外れたりするため落とす。
_SENTENCE_LEAD_RE: Final[Any] = re.compile(r"\A(?:[-*+>#|]+|\d+[.)])\s*")

# 文の意味を運ばない書式。**書式で文が近くなると、無関係な文書どうしが重なる。** 実測で、表の
# 区切りの行、強調の印だけが違う見出し、文書間の参照の並びが、無関係な文書の合併に対して重なった
# 文のほとんどを占めた。参照の並びは参照先の名前を列挙しているだけで、指示の意味を持たない。
# 印を 1 つずつ足さず、書式の記号をまとめて落とし、残った文字のうち字の数で長さを測る。
_SENTENCE_MARKUP_RE: Final[Any] = re.compile(r"\[\[[^\]\n]*\]\]|\]\([^)\s]*\)|[*_`|~]+")

_SEMANTIC_PLANE_LABEL: Final[bytes] = b"senda-argus/instruction-semantic-planes/v1|"
_SEMANTIC_BAND_LABEL: Final[bytes] = b"senda-argus/instruction-semantic-band/v1|"

# 超平面は鍵と次元ごとに一度だけ作る。鍵そのものは控えの名前に使わず、鍵のダイジェストを使う。
_SEMANTIC_PLANE_CACHE: Final[dict[tuple[bytes, int], list[list[float]]]] = {}
_SEMANTIC_PLANE_CACHE_MAX: Final[int] = 4

# 指示側の結果の控え。指示は毎回の呼び出しで同じ本文が載るため、同じ本文を何度も埋め込まない。
_SEMANTIC_RESULT_CACHE: Final[dict[tuple[bytes, int, str], list[str]]] = {}
_SEMANTIC_RESULT_CACHE_MAX: Final[int] = 64

_SEMANTIC_LOCK: Final[Any] = threading.Lock()

# 埋め込みの取得先。差し替えたものがあればそれを使い、無ければ環境変数のディレクトリから読む。
# 読めなかったことも控え、呼び出しのたびに読み直さない。
_SEMANTIC_EMBEDDER: Final[dict[str, Any]] = {
    "override": None,
    "loaded": False,
    "model": None,
}


def set_sentence_embedder(embedder: Any) -> None:
    """文の列を受けて同じ長さのベクトルの列を返す関数を差し替える。None で既定の読み込みへ戻す。"""
    with _SEMANTIC_LOCK:
        _SEMANTIC_EMBEDDER["override"] = embedder
        _SEMANTIC_EMBEDDER["loaded"] = False
        _SEMANTIC_EMBEDDER["model"] = None
        _SEMANTIC_RESULT_CACHE.clear()


def reset_semantic_signature_for_tests() -> None:
    """差し替えと読み込みの結果と控えを全部消す。"""
    set_sentence_embedder(None)
    with _SEMANTIC_LOCK:
        _SEMANTIC_PLANE_CACHE.clear()


def _load_local_embedder() -> Any:
    """環境変数のディレクトリから埋め込みモデルを読む。読めなければ None を返す。

    **外部へ接続しない。** 実在するローカルのディレクトリだけを受け付け、取得を禁じる印を立て、
    読み込みもローカルのファイルに限る。モデルの中のコードは実行しない。ライブラリが入っていない
    構成では何も読まず、文の署名を出さない。
    """
    directory = os.environ.get(SEMANTIC_MODEL_DIR_ENV, "").strip()
    if not directory or not os.path.isdir(directory):
        return None
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        from sentence_transformers import SentenceTransformer
    except Exception:  # noqa: BLE001
        return None
    try:
        model = SentenceTransformer(
            directory,
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
        )
    except Exception:  # noqa: BLE001
        return None

    def _encode(sentences: list[str]) -> Any:
        return model.encode(
            sentences,
            convert_to_numpy=True,
            normalize_embeddings=False,
            show_progress_bar=False,
        ).tolist()

    return _encode


def _resolve_embedder() -> Any:
    with _SEMANTIC_LOCK:
        override = _SEMANTIC_EMBEDDER["override"]
        if override is not None:
            return override
        if not _SEMANTIC_EMBEDDER["loaded"]:
            _SEMANTIC_EMBEDDER["model"] = _load_local_embedder()
            _SEMANTIC_EMBEDDER["loaded"] = True
        return _SEMANTIC_EMBEDDER["model"]


def semantic_signature_key() -> bytes | None:
    """テナントの鍵を環境変数から読む。形が合わなければ None を返し、署名を出さない。"""
    raw = os.environ.get(SEMANTIC_KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(key) < SEMANTIC_KEY_MIN_BYTES:
        return None
    return key


def split_sentences(text: Any) -> list[str]:
    """本文を文に分ける。互換の字形へ均し、書式を落とし、空白を詰め、短い文を除き、重複を畳む。"""
    if not isinstance(text, str) or not text:
        return []
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    seen: set[str] = set()
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = _SENTENCE_MARKUP_RE.sub(" ", _SENTENCE_LEAD_RE.sub("", raw.strip()))
        sentence = " ".join(_SENTENCE_LEAD_RE.sub("", sentence.strip()).split())
        # 長さは字の数で測る。記号や数字だけが並ぶ行は、長くても文の意味を持たない。
        if sum(1 for ch in sentence if ch.isalpha()) < SEMANTIC_MIN_SENTENCE_CHARS:
            continue
        sentence = sentence[:SEMANTIC_MAX_SENTENCE_CHARS]
        if sentence in seen:
            continue
        seen.add(sentence)
        out.append(sentence)
    return out


def _select_sentences(sentences: list[str], limit: int) -> tuple[list[str], bool]:
    """上限を超える分を、本文の位置に依らない値の順で落とす。

    **文頭から詰めて打ち切ると、末尾に書かれたものが必ず落ちる。** 指示ファイルは追記して育つため、
    後から書かれた払い出しがちょうど落ちる位置に来る。文のダイジェストの順で選ぶ。
    """
    ordered = sorted(sentences, key=lambda s: hashlib.sha256(s.encode("utf-8")).digest())
    return ordered[:limit], len(ordered) > limit


def _semantic_planes(key: bytes, dim: int) -> list[list[float]]:
    """鍵と次元から超平面を作る。鍵から決まるため、両側で同じ超平面になる。

    各成分は鍵付きの擬似乱数から正規分布に従う値を作る。正規分布の超平面で符号を取ると、
    2 つのベクトルの符号が一致する割合が両者のなす角で決まる。
    """
    cache_key = (hashlib.sha256(key).digest(), dim)
    with _SEMANTIC_LOCK:
        cached = _SEMANTIC_PLANE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    count = SEMANTIC_BANDS * SEMANTIC_ROWS * dim
    values: list[float] = []
    counter = 0
    while len(values) < count:
        block = hmac.new(
            key,
            _SEMANTIC_PLANE_LABEL + struct.pack(">IQ", dim, counter),
            hashlib.sha256,
        ).digest()
        counter += 1
        for offset in (0, 16):
            high, low = struct.unpack(">QQ", block[offset : offset + 16])
            radius = math.sqrt(-2.0 * math.log((high + 1) / 18446744073709551617.0))
            angle = 2.0 * math.pi * (low / 18446744073709551616.0)
            values.append(radius * math.cos(angle))
            values.append(radius * math.sin(angle))
    planes = [values[i * dim : (i + 1) * dim] for i in range(SEMANTIC_BANDS * SEMANTIC_ROWS)]
    with _SEMANTIC_LOCK:
        if len(_SEMANTIC_PLANE_CACHE) >= _SEMANTIC_PLANE_CACHE_MAX:
            _SEMANTIC_PLANE_CACHE.clear()
        _SEMANTIC_PLANE_CACHE[cache_key] = planes
    return planes


def _valid_vectors(vectors: Any, count: int) -> list[list[float]] | None:
    """埋め込みの戻り値を確かめる。数と次元と値の形が合わなければ None を返す。"""
    try:
        rows = [list(v) for v in vectors]
    except TypeError:
        return None
    if len(rows) != count or not rows:
        return None
    dim = len(rows[0])
    if not _SEMANTIC_MIN_DIM <= dim <= _SEMANTIC_MAX_DIM:
        return None
    out: list[list[float]] = []
    for row in rows:
        if len(row) != dim:
            return None
        values: list[float] = []
        for value in row:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            if not math.isfinite(value):
                return None
            values.append(float(value))
        out.append(values)
    return out


def _signature_bits(planes: list[list[float]], vectors: list[list[float]]) -> list[list[bool]]:
    """各ベクトルが各超平面のどちら側にあるかを返す。

    数値計算のライブラリがあればそれで掛ける。埋め込みのモデルを読む構成には必ず入っている。
    無ければ同じ計算を素の Python で行う。両者の差は内積が 0 にごく近いときの符号だけで、
    帯のどれか 1 つが揃えばよい突合には効かない。
    """
    try:
        import numpy
    except Exception:  # noqa: BLE001
        numpy = None
    if numpy is not None:
        products = numpy.asarray(vectors, dtype=numpy.float64) @ numpy.asarray(
            planes, dtype=numpy.float64
        ).T
        return [[bool(v) for v in row] for row in (products >= 0.0).tolist()]
    return [
        [sum(p * v for p, v in zip(plane, vector)) >= 0.0 for plane in planes]
        for vector in vectors
    ]


def _band_digests(key: bytes, bits: list[bool]) -> list[str]:
    """1 文の符号を帯に分け、帯ごとの鍵付きダイジェストにする。"""
    out: list[str] = []
    for band in range(SEMANTIC_BANDS):
        value = 0
        for bit in bits[band * SEMANTIC_ROWS : (band + 1) * SEMANTIC_ROWS]:
            value = (value << 1) | (1 if bit else 0)
        mac = hmac.new(
            key,
            _SEMANTIC_BAND_LABEL
            + struct.pack(">BB", band, SEMANTIC_ROWS)
            + value.to_bytes((SEMANTIC_ROWS + 7) // 8, "big"),
            hashlib.sha256,
        ).hexdigest()
        out.append(SEMANTIC_DIGEST_PREFIX + mac)
    return out


def semantic_band_digests_with_overflow(
    body: Any,
    *,
    limit: int = MAX_SEMANTIC_SENTENCES,
    is_patch: bool = False,
    embedder: Any = None,
    key: bytes | None = None,
) -> tuple[list[str], bool]:
    """本文を文ごとの帯のダイジェストにする。文の数が上限を超えたかも返す。

    返す列は文ごとに帯の数ずつ並ぶ。受け取り側はこの単位で重なる文を数える。埋め込みか鍵が
    無いとき、埋め込みの戻り値が壊れているときは空を返す。
    """
    body = normalize_patch_body(body, is_patch=is_patch)
    sentences = split_sentences(body)
    if not sentences:
        return [], False
    key = key if key is not None else semantic_signature_key()
    embed = embedder if embedder is not None else _resolve_embedder()
    if key is None or embed is None:
        return [], False
    chosen, overflowed = _select_sentences(sentences, limit)
    started = time.monotonic()
    out: list[str] = []
    planes: list[list[float]] | None = None
    for start in range(0, len(chosen), SEMANTIC_BATCH):
        if out and time.monotonic() - started > SEMANTIC_MAX_SECONDS:
            break
        batch = chosen[start : start + SEMANTIC_BATCH]
        try:
            vectors = _valid_vectors(embed(batch), len(batch))
        except Exception:  # noqa: BLE001
            return [], False
        if vectors is None:
            return [], False
        # 塊ごとに次元が変わる戻り値は壊れている。別の次元の超平面で作った値を混ぜない。
        if planes is None:
            planes = _semantic_planes(key, len(vectors[0]))
        elif len(planes[0]) != len(vectors[0]):
            return [], False
        for bits in _signature_bits(planes, vectors):
            out.extend(_band_digests(key, bits))
    return out, overflowed


def semantic_band_digests(body: Any, **options: Any) -> list[str]:
    """本文を文ごとの帯のダイジェストにする。"""
    return semantic_band_digests_with_overflow(body, **options)[0]


def system_prompt_semantic_digests(*sources: Any, **named: Any) -> list[str]:
    """指示の本文を、文ごとの帯のダイジェストにする。入力の受け方は行と組の導出と揃える。

    同じ本文は控えから返す。指示は毎回の呼び出しに同じ本文が載り、埋め込みは時間を使う。
    控えの名前には鍵のダイジェストと埋め込みの取得先を含め、鍵や取得先を替えたら引き直す。
    """
    texts: list[str] = []
    for source in list(sources) + list(named.values()):
        texts.extend(_texts_from(source))
    texts = [t for t in texts if t]
    if not texts:
        return []
    key = semantic_signature_key()
    embed = _resolve_embedder()
    if key is None or embed is None:
        return []
    joined = "\n".join(texts)
    cache_key = (
        hashlib.sha256(key).digest(),
        id(embed),
        hashlib.sha256(joined.encode("utf-8")).hexdigest(),
    )
    with _SEMANTIC_LOCK:
        cached = _SEMANTIC_RESULT_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)
    digests = semantic_band_digests(joined, embedder=embed, key=key)
    with _SEMANTIC_LOCK:
        if len(_SEMANTIC_RESULT_CACHE) >= _SEMANTIC_RESULT_CACHE_MAX:
            _SEMANTIC_RESULT_CACHE.clear()
        _SEMANTIC_RESULT_CACHE[cache_key] = list(digests)
    return digests
