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
import re
import posixpath
from typing import Any, Final, Optional

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

# 実測で、3 つの計画の文書 195 件のうち、64 では 121 件しか全体を運べない。256 なら 188 件が
# 収まり、1 件あたりの大きさは 17 キロバイトに収まる。上限を超える本文では、一部だけを見た
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

# 同じ行の中で、いくつ先の語まで組にするか。要約は文の順序を変えるが、同じ文に現れた語どうしは
# 近くに残りやすい。窓を広げても分離は変わらず、組の数だけが増える。
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
_TOKEN_RE: Final[Any] = re.compile(r"[A-Za-z0-9_.\\/:@~?#%&=+\[\]-]+")

# 大小を無視してよい部分。**経路の大小は意味を持つ。** 語をまるごと小文字へ倒すと、
# /srv/TenantA と /srv/tenanta が同じダイジェストになり、別の対象を指す組が一致する。
# URL のホスト名とスキームだけを倒し、あわせてドライブ文字から始まる経路も倒す。前者は綴りが大小を区別せず、
# 後者はその環境の経路そのものが大小を区別しない。
_SCHEME_SEP: Final[str] = "://"
_DRIVE_RE: Final[Any] = re.compile(r"\A[A-Za-z]:")


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
# 並んで現れる。実測で、3 つの計画の文書 195 件を総当たりした 18905 組のうち 54 組が 2 組以上
# 重なった。位置を指す語に限ると 2 組以上は 13 組、3 組以上は 0 組になる。
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
    if _SCHEME_SEP in token:
        return True
    if _DRIVE_RE.match(token):
        return True
    return token.startswith(_ROOTED_PREFIXES)



def _digest(value: str) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()


def instruction_file_name(path: Any) -> Optional[str]:
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


_DIFF_MARKERS: Final[tuple[str, ...]] = ("@@ ", "--- ", "+++ ")


def _looks_like_patch(body: str) -> bool:
    """本文が差分形式かどうかを、位置情報の行の有無で判定する。"""
    for raw in body.splitlines():
        if raw.startswith(_DIFF_MARKERS):
            return True
    return False


def normalize_patch_body(body: Any) -> Any:
    """差分形式の本文を、適用後に残る文言へ均す。

    書き込みが差分で渡された場合、行の先頭に付く記号を落とさずにダイジェストへ通すと、後から
    指示に現れる同じ行と一致しない。指示側には記号の付かない行が載るためである。差分でない
    本文はそのまま返す。

    削除の行は適用後に残らないため落とす。位置情報の行も本文ではないため落とす。
    """
    if not isinstance(body, str) or not body or not _looks_like_patch(body):
        return body
    kept: list[str] = []
    for raw in body.splitlines():
        if raw.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+"):
            kept.append(raw[1:])
            continue
        if raw.startswith(" "):
            kept.append(raw[1:])
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

    __slots__ = ("_limit", "_slack", "_seen", "_ceiling")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._slack = max(limit * 4, limit + 1)
        self._seen: set[str] = set()
        self._ceiling: Optional[str] = None

    def add(self, digest: str) -> None:
        if self._ceiling is not None and digest > self._ceiling:
            return
        self._seen.add(digest)
        if len(self._seen) > self._slack:
            self._prune()

    def _prune(self) -> None:
        kept = sorted(self._seen)[: self._limit]
        self._seen = set(kept)
        self._ceiling = kept[-1] if kept else None

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


def line_digests(body: Any, *, limit: int = MAX_LINE_DIGESTS) -> list[str]:
    """本文を正規化した行ごとのダイジェストにする。

    前後の空白を落として空行を除く。短い行は無関係な文書どうしでも一致するため除く。同じ行が
    繰り返されても 1 つに畳む。出現順は保たず、集合として扱う。差分形式の本文は、適用後に残る
    文言へ均してから通す。
    """
    body = normalize_patch_body(body)
    if not isinstance(body, str) or not body:
        return []
    seen = BoundedDigestSet(limit)
    for raw in body.splitlines():
        line = raw.strip()
        if len(line) < MIN_LINE_LENGTH:
            continue
        seen.add(_digest(line))
    return seen.result()


def _distinctive_tokens(text: str) -> list[str]:
    """1 行から、位置を指す語だけを取り出す。

    経路と URL に限る。**語の一覧は持たない。** 一覧は言語ごとに要り、維持できない。区切りを
    含むことと長さだけなら、どの言語でも同じ手続きで決まる。

    取り出せるのは ASCII で書かれた経路と URL に限られる。日本語だけで書かれた指示ファイルからは
    組が出ない。その構成では行ごとの突合だけが働く。
    """
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        # バックスラッシュはスラッシュへ均す。同じ対象を指す経路が、環境の書き方の違いだけで
        # 別のダイジェストになると突合が成立しない。
        #
        # **先頭の区切りは落とさない。** 落とすと /srv/a と srv/a が同じダイジェストになり、
        # 起点の違う別の対象を指す組が一致する。落とすのは文の側の記号だけにする。
        # **末尾の点は落とさない。** 経路の一部でありうるため、落とすと /srv/a. と /srv/a が
        # 同じダイジェストになり、別の対象を指す組が一致する。落とすのは経路の末尾に来ない
        # 記号だけにする。
        token = raw.replace("\\", "/").rstrip(",;:~@/?#&").lstrip("-:@")
        # 角括弧が意味を持つのは、種別の直後に来る宛先の中だけである。文の側の括弧を語へ
        # 取り込むと、同じ経路が囲まれているかどうかで別のダイジェストになる。
        if _SCHEME_SEP not in token:
            token = token.strip("[]")
        token = _fold_case(token)
        if len(token) < MIN_TOKEN_LENGTH:
            continue
        if _TOKEN_LOCATOR not in token:
            continue
        # **起点の無い表記は、指す先を定めない。** 文書の中の相互参照はこの形を取り、
        # 同じ計画の文書どうしが同じ綴りを共有する。指す先が同じとは限らないため証拠にならない。
        if not _is_rooted(token):
            continue
        out.append(token)
    return out


def token_pair_digests(body: Any, *, limit: int = MAX_PAIR_DIGESTS) -> list[str]:
    """本文を、同じ行に現れた珍しい語の組ごとのダイジェストにする。

    要約を経ると行はそのまま残らないが、払い出しが指す先、すなわち経路や URL は書き換えられずに
    残る。**1 語では足りない。** 同じ計画の文書どうしは語彙を共有するため、語 1 つの一致は
    無関係な文書の間でも起きる。組にすると、組み合わせが一致する確率は大きく下がる。実測で、
    3 つの計画の文書 195 件を総当たりした 18905 組のうち、3 組以上重なったものは無かった。

    **組は同じ行の中でだけ作る。** 連続する行をまとめると、同じ計画の文書どうしで 3 組以上の
    重なりが出る。実測で連続 2 行をまとめた場合は 12 組、本文全体では 28 組が 3 組以上重なった。

    組は並べ替えてから作る。要約は語の順序を変えるため、順序を含めると突合が成立しない。

    本文そのものは返さない。内容を運ばずに突合できる形だけを出す。
    """
    body = normalize_patch_body(body)
    if not isinstance(body, str) or not body:
        return []
    seen = BoundedDigestSet(limit)
    for raw in body.splitlines():
        tokens = _distinctive_tokens(raw)
        for i in range(len(tokens)):
            for j in range(i + 1, min(i + 1 + TOKEN_PAIR_WINDOW, len(tokens))):
                left, right = sorted((tokens[i], tokens[j]))
                if left == right:
                    continue
                seen.add(_digest(f"{left}\x1f{right}"))
    return seen.result()


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


def _first_present(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def classify_instruction_write(arguments: Any) -> Optional[dict[str, Any]]:
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
    body = _first_present(arguments, _BODY_KEYS)
    if not isinstance(body, str) or not body:
        return None
    return {
        "instruction_file_name": name,
        "written_content_hash": _digest(body),
        # 書き込み側は落とさない。落とすと、書き手が埋め草で払い出しを押し出せる。
        "written_line_hashes": line_digests(body, limit=MAX_WRITE_DIGESTS),
        "written_pair_hashes": token_pair_digests(body, limit=MAX_WRITE_DIGESTS),
    }


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


def _flatten_batch(value: Any) -> Any:
    """束ねられた列を 1 段ほどく。

    枠組みによっては、1 回の要求に複数の会話を束ねて渡す。外側の列は役割を持たないため、
    ほどかずに渡すと役割の判定も本文の取り出しも成立せず、指示が 1 件も拾えない。
    """
    if not isinstance(value, (list, tuple)) or not value:
        return value
    if all(isinstance(item, (list, tuple)) for item in value):
        return [inner for item in value for inner in item]
    return value


def _declares_role(value: Any) -> bool:
    """役割を宣言した要素を含む列かどうかを返す。"""
    if not isinstance(value, (list, tuple)):
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
    if not isinstance(parts, (list, tuple)):
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
    if isinstance(source, (list, tuple)):
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
            elif isinstance(content, (list, tuple)):
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
    if isinstance(positional, (list, tuple)):
        for item in positional:
            if isinstance(item, (list, tuple)):
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
