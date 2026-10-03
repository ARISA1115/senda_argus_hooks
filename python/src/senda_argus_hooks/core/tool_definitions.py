"""MCP の ``list_tools`` の応答から、ツールごとの定義のダイジェストを作る。

Argus は登録された提供元へ自分で接続して同じ一覧を取得し、エージェントが受け取った定義と突き合わせる。
呼び出し元を見分けて、監視の側には無害な定義を、エージェントには細工した定義を返す提供元を捉えるため。
突き合わせはダイジェストの一致で行うため、**正規化の規則は Argus の側と一字一句同じでなければならない。**

**直列化は言語の既定に任せない。** Python の ``json.dumps`` と JS の ``JSON.stringify`` は、数の表記、
鍵の並び、孤立したサロゲートの扱いが違う。同じ定義が言語によって別の値になると、すべての提供元が
見せ方を変えているように見える。そのため次の規則を両方の言語で自前に持つ。

- 対象は ``name`` と ``description`` と ``inputSchema`` の 3 項目に限る。文字列でない ``description`` と、
  辞書でない ``inputSchema`` は ``null`` に置く
- 数は ECMAScript の Number の文字列化と同じ表記にする。2 の 53 乗を超える整数は浮動小数を経て表す。
  JSON に無い数は ``null`` に置く
- 鍵はコードポイントの順に並べる。JS の既定の並べ替えは UTF-16 の単位の順で、補助面の文字で順が変わる
- 文字列は ``JSON.stringify`` と同じ規則で逃がす。制御文字と孤立したサロゲートは小文字の ``\\u`` で表す
- 区切りに空白を入れず、UTF-8 にして SHA-256 を取る

提供元の URL も同じ規則で正規化する。スキームとホストを小文字にし、資格情報の部分を落とし、既定の
ポートを落とし、ASCII でないホストの区間を Punycode にし、パスの ``.`` と ``..`` を解き、末尾の区切りと
問い合わせと断片を落とす。読めない URL は ``None`` を返す。推測した値で鍵を作らない。

JS の実装 (js/src/core/tool_definitions.ts) も同じ規則を持つ。規則を変えるときは両方を変え、両方の
テストが読む共通の例 (js/tests/fixtures/mcp_tool_definition_hashes.json) を更新する。

**保持には上限がある。** 1 回の応答から作るダイジェストはツールの名前の台帳と同じ件数までにする。
名前は台帳と同じ規則で畳む。
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import Any

from .mcp_tools import MAX_TOOLS_PER_SERVER, fold_name

_MAX_SAFE_INTEGER = 2**53
_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _number(value: float) -> str:
    """ECMAScript の Number の文字列化と同じ表記。"""
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    mantissa, _, exp_text = repr(abs(value)).partition("e")
    int_part, _, frac_part = mantissa.partition(".")
    digits = int_part + frac_part
    point = len(int_part) + (int(exp_text) if exp_text else 0)
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0") or "0"
    k, n = len(digits), point
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        text = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + text


def _string(value: str) -> str:
    """``JSON.stringify`` と同じ規則で文字列を逃がす。隣り合う上位と下位のサロゲートは 1 文字に戻す。"""
    out: list[str] = ['"']
    i = 0
    length = len(value)
    while i < length:
        ch = value[i]
        code = ord(ch)
        if 0xD800 <= code <= 0xDBFF and i + 1 < length and 0xDC00 <= ord(value[i + 1]) <= 0xDFFF:
            out.append(chr(0x10000 + ((code - 0xD800) << 10) + (ord(value[i + 1]) - 0xDC00)))
            i += 2
            continue
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif code < 0x20 or 0xD800 <= code <= 0xDFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
        i += 1
    out.append('"')
    return "".join(out)


def canonical_json(value: Any) -> str:
    """言語に依らず同じ文字列になる JSON。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) <= _MAX_SAFE_INTEGER:
            return str(value)
        try:
            return _number(float(value))
        except OverflowError:
            return "null"
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, dict):
        items = sorted(((str(k), v) for k, v in value.items()), key=lambda kv: _sort_key(kv[0]))
        return "{" + ",".join(_string(k) + ":" + canonical_json(v) for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def _sort_key(key: str) -> list[int]:
    """コードポイントの順。隣り合う上位と下位のサロゲートは 1 つのコードポイントとして並べる。"""
    points: list[int] = []
    i = 0
    while i < len(key):
        code = ord(key[i])
        if 0xD800 <= code <= 0xDBFF and i + 1 < len(key) and 0xDC00 <= ord(key[i + 1]) <= 0xDFFF:
            points.append(0x10000 + ((code - 0xD800) << 10) + (ord(key[i + 1]) - 0xDC00))
            i += 2
            continue
        points.append(code)
        i += 1
    return points


def _field(tool: Any, name: str) -> Any:
    if isinstance(tool, dict):
        return tool.get(name)
    return getattr(tool, name, None)


def tool_definition_hash(tool: Any) -> str | None:
    """1 つのツールの定義のダイジェスト。名前を持たないツールは None を返す。"""
    name = _field(tool, "name")
    if not isinstance(name, str) or not name:
        return None
    description = _field(tool, "description")
    schema = _field(tool, "inputSchema")
    payload = canonical_json(
        {
            "name": name,
            "description": description if isinstance(description, str) else None,
            "inputSchema": schema if isinstance(schema, dict) else None,
        }
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def tool_definition_hashes(response: Any) -> dict[str, str]:
    """``list_tools`` の応答から、畳んだ名前とダイジェストの対応を作る。

    応答は ``tools`` を持つ SDK の応答、辞書の応答、ツールの並びそのものを受ける。上限を超えた分は載せない。
    """
    tools = getattr(response, "tools", None)
    if tools is None and isinstance(response, dict):
        tools = response.get("tools")
    if tools is None and isinstance(response, (list, tuple)):
        tools = response
    hashes: dict[str, str] = {}
    if not isinstance(tools, (list, tuple)):
        return hashes
    for tool in tools:
        if len(hashes) >= MAX_TOOLS_PER_SERVER:
            break
        try:
            digest = tool_definition_hash(tool)
        except (TypeError, ValueError, RecursionError):
            # 直列化できない定義は載せない。既定のダイジェストを当てると、別々の定義が同じ値になる
            continue
        if digest is None:
            continue
        hashes[fold_name(str(_field(tool, "name")))] = digest
    return hashes


_URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://([^/?#]*)([^?#]*)")
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 の 5.2.4 の手順でパスの ``.`` と ``..`` を解く。"""
    output: list[str] = []
    for segment in path.split("/"):
        if not segment:
            continue
        if segment == ".":
            continue
        if segment == "..":
            if output:
                output.pop()
            continue
        output.append(segment)
    # 末尾の区切りは呼び出し側が落とすため、ここでは付け直さない
    return "/" + "/".join(output)


def _host_label(label: str) -> str:
    if label.isascii():
        return label
    return "xn--" + label.encode("punycode").decode("ascii")


def normalize_provider_url(url: Any) -> str | None:
    """提供元の URL を突き合わせの形へ正規化する。読めない URL は None を返す。"""
    if not url:
        return None
    match = _URL_RE.match(str(url).strip())
    if not match:
        return None
    scheme = match.group(1).lower()
    authority = match.group(2).rpartition("@")[2]
    if authority.startswith("["):
        host, sep, rest = authority.partition("]")
        if not sep:
            return None
        host = host + "]"
        port = rest[1:] if rest.startswith(":") else ""
        if rest and not rest.startswith(":"):
            return None
    else:
        host, sep, port = authority.rpartition(":")
        if not sep:
            host, port = authority, ""
    if port and not port.isdigit():
        return None
    port = str(int(port)) if port else ""
    if port == _DEFAULT_PORTS.get(scheme):
        port = ""
    host = unicodedata.normalize("NFC", host).lower()
    if not host or host == "[]":
        return None
    try:
        host = ".".join(_host_label(label) for label in host.split("."))
    except UnicodeError:
        return None
    path = _remove_dot_segments(match.group(3) or "/").rstrip("/") or "/"
    return f"{scheme}://{host}{':' + port if port else ''}{path}"
