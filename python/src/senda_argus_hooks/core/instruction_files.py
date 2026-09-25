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

MAX_LINE_DIGESTS: Final[int] = 64

# 突合の対象にする行の最小の長さ。短い行は無関係な文書どうしでも一致するため、集合の重なりが
# 伝播の証拠にならなくなる。
MIN_LINE_LENGTH: Final[int] = 24

_DIGEST_PREFIX: Final[str] = "sha256:"


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


def line_digests(body: Any) -> list[str]:
    """本文を正規化した行ごとのダイジェストにする。

    前後の空白を落として空行を除く。短い行は無関係な文書どうしでも一致するため除く。同じ行が
    繰り返されても 1 つに畳む。出現順は保たず、集合として扱う。差分形式の本文は、適用後に残る
    文言へ均してから通す。
    """
    body = normalize_patch_body(body)
    if not isinstance(body, str) or not body:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if len(line) < MIN_LINE_LENGTH:
            continue
        d = _digest(line)
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
        if len(out) >= MAX_LINE_DIGESTS:
            break
    return out


def _first_present(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def classify_instruction_write(arguments: Any) -> Optional[dict[str, Any]]:
    """指示ファイルへの書き込みなら、突合に使う情報を返す。該当しなければ None。

    返すのは、一覧に載っている名前と、本文全体のダイジェストと、行ごとのダイジェストである。
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
        "written_line_hashes": line_digests(body),
    }


# 指示にあたる本文が載る引数の名前。提供元ごとに異なる。役割つきの列に載る場合と、独立した
# 引数で渡る場合と、オブジェクトの属性として保持される場合がある。
_INSTRUCTION_KEYS: Final[tuple[str, ...]] = (
    "system", "system_instruction", "instructions", "systemInstruction",
)

# 役割つきの要素が載る引数の名前。応答系の要求では input に載る。
_ROLE_LIST_KEYS: Final[tuple[str, ...]] = ("messages", "input", "contents")


def _block_text(block: Any) -> str:
    """種別つきの塊から本文を取り出す。形は提供元ごとに異なる。"""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        for key in ("text", "content", "input_text"):
            value = block.get(key)
            if isinstance(value, str):
                return value
        return ""
    text = getattr(block, "text", None)
    return text if isinstance(text, str) else ""


def _role_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("role") or "").strip().lower()
    return str(getattr(item, "role", "") or "").strip().lower()


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
                if _role_of(item) != SYSTEM_ROLE:
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
            if call_kwargs.get(key) is not None:
                sources.append(call_kwargs[key])
    if isinstance(positional, (list, tuple)):
        for item in positional:
            if isinstance(item, (list, tuple)):
                sources.append(item)
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
