"""端末の MCP サーバを導入の前に走査し、結果を Argus の POST /v1/install-scans へ送るコマンド。

既定は起動しない静的な方式に限る。stdio のサーバのプロセスを起動せず、リモートのサーバへも接続しない。
走査は、事前に取得した tools の JSON を渡したサーバだけを、外部のコマンド mcp-scanner の static の
サブコマンドと YARA の解析器で行う。JSON が無いサーバと、走査器を動かせなかったサーバは、所見なしと
偽らず、未走査として理由を付けて送る。

送る物は導入物の実体の材料 (名前、コマンド、引数、パッケージ名と版、ローカルのファイルの sha256、URL) と
走査器の出力だけにする。設定の env と headers の値と、引数に埋め込まれた秘密は送らず、表示もしない。
実体の鍵は受け側が計算する。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from senda_argus_hooks.cli.mcp_configs import ConfiguredServer, read_configured_servers
from senda_argus_hooks.core.device_identity import sign_headers
from senda_argus_hooks.core.tool_definitions import normalize_provider_url

SCANNER = "mcp-scanner"
SCAN_PATH = "/v1/install-scans"
REDACTED = "<redacted>"

# 受け側の未走査の理由。受け側の UNSCANNED_REASONS と同じ値。
REASON_TIMEOUT = "timeout"
REASON_ANALYZER_DISABLED = "analyzer_disabled"

# 受け側の上限。超える値は送らずに、送る前に失敗として出す。
MAX_ARGS = 64
MAX_ARG_LEN = 1024
MAX_COMMAND_LEN = 1024
MAX_NAME_LEN = 256
MAX_PACKAGE_LEN = 256
MAX_VERSION_LEN = 128
MAX_URL_LEN = 2048
MAX_TOOLS = 256
MAX_TOOL_DEFINITION_BYTES = 16384
MAX_TOOLS_TOTAL_BYTES = 262144
# ハッシュを取るファイルの大きさの上限。超えるファイルは読まず、ハッシュを省く。
MAX_HASH_BYTES = 256 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024
# 走査器の出力と、受け側の応答を読む量の上限。
MAX_SCANNER_OUTPUT = 16 * 1024 * 1024
MAX_TOOLS_FILE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


# --- 秘密の除去 -------------------------------------------------------------------------

# 名前にこれらを含む項目の値は秘密とみなす。名前は英数字以外を除き小文字にして比べる。
_SECRET_NAME_PARTS = (
    "token",
    "secret",
    "passw",
    "passwd",
    "apikey",
    "accesskey",
    "privatekey",
    "credential",
    "bearer",
    "auth",
    "cookie",
    "session",
    "signature",
)
# 名前を語に分けたとき、語がこれらに一致するなら秘密とみなす。部分一致にすると無関係な名前に当たる語。
_SECRET_NAME_WORDS = frozenset(
    {"key", "pat", "pwd", "pass", "pin", "header", "headers"}
)
# 値の形から秘密とみなすトークンの形。値のどこに現れても、引数ごと除く。語の途中の ask- などに当てず、
# 接頭辞の後にトークンの文字が続くときだけ当てる。
_SECRET_VALUE_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:"
    r"(?i:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|sk-|sk_live_|sk_test_|rk_live_|xoxb-|xoxp-|xoxa-|xoxs-|xapp-|npm_|pypi-|hf_)[A-Za-z0-9_\-]{8,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|AIza[A-Za-z0-9_\-]{30,}"
    r"|ya29\.[A-Za-z0-9_\-]{8,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    r"|(?i:bearer|basic)\s+\S+"
    r")"
)
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_\-+=.~]{32,}$")
_URL_RE = re.compile(
    r"^([a-z][a-z0-9+.\-]*)://([^/?#]*)([^?#]*)(\?[^#]*)?(#.*)?$", re.IGNORECASE
)


def _name_is_secret(name: str) -> bool:
    lowered = name.lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    if any(part in compact for part in _SECRET_NAME_PARTS):
        return True
    words = [w for w in re.split(r"[^a-z0-9]+", lowered) if w]
    return any(w in _SECRET_NAME_WORDS for w in words)


_HEX_RE = re.compile(r"^[0-9A-Fa-f]{32,}$")
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_MIXED_RE = re.compile(r"^[A-Za-z0-9_\-]{20,}$")


def _looks_opaque(value: str) -> bool:
    """乱数の鍵とトークンの形か。

    16 進の 32 文字以上と UUID は、文字の種類が少なくエントロピーの判定に届かないため、字面で当てる。
    英大文字と英小文字と数字が混ざる 20 文字以上の値も当てる。npm のパッケージ名は小文字に限られる。
    """
    if _HEX_RE.match(value) or _UUID_RE.match(value):
        return True
    if (
        _MIXED_RE.match(value)
        and not value.startswith("-")
        and re.search(r"[A-Z]", value)
        and re.search(r"[a-z]", value)
        and re.search(r"[0-9]", value)
    ):
        return True
    if not _OPAQUE_RE.match(value):
        return False
    if not (re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value)):
        return False
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = -sum(c / len(value) * math.log2(c / len(value)) for c in counts.values())
    return entropy >= 4.0


def _redact_url(value: str) -> str | None:
    """URL の利用者情報とクエリとフラグメントを除く。URL でなければ None。"""
    match = _URL_RE.match(value)
    if not match:
        return None
    scheme, authority, path, query, fragment = match.groups()
    if "@" in authority:
        authority = REDACTED + "@" + authority.rpartition("@")[2]
    # パスの区切りに入った秘密 (Webhook の URL など) は、その区切りだけを除く。
    path = "/".join(
        REDACTED if segment and _value_is_secret(segment) else segment
        for segment in path.split("/")
    )
    out = f"{scheme}://{authority}{path}"
    if query:
        out += "?" + REDACTED
    if fragment:
        out += "#" + REDACTED
    return out


def _value_is_secret(value: str) -> bool:
    if _SECRET_VALUE_RE.search(value):
        return True
    return _looks_opaque(value)


def _assignment_name(value: str) -> str | None:
    """名前=値 と 名前: 値 の名前。URL の scheme の後の :// は名前の区切りにしない。"""
    match = re.match(r"^\s*-{0,2}([A-Za-z0-9_.\-]+)\s*(=|:(?!//))", value)
    return match.group(1) if match else None


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if _name_is_secret(str(k)) else _redact_json(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(v) for v in value]
    if isinstance(value, str):
        return redact_arg(value)
    return value


_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s'\"]*@")
_PIECE_SPLIT_RE = re.compile(r"[;&\s]+")
_QUOTES = "'\"`"


def _pieces_hold_secret(value: str) -> bool:
    """値を ;、&、空白で分けた各片に、名前の判定と値の形の判定を当てる。

    接続文字列の Password=... と、コマンドの文字列の中の --api-key VALUE の形を見る。URL の片は
    パスの区切りごとに別に見るため、ここでは値の形の判定を当てない。
    """
    # 名前 = 値 のように区切りの前後に空白を置く書き方も 1 つの片にする。
    joined = re.sub(r"\s*([=:])\s*", r"\1", value)
    pieces = [
        p.strip(_QUOTES) for p in _PIECE_SPLIT_RE.split(joined) if p.strip(_QUOTES)
    ]
    for index, piece in enumerate(pieces):
        name = _assignment_name(piece)
        if name is not None and _name_is_secret(name):
            return True
        if (
            piece.startswith("-")
            and "=" not in piece
            and _name_is_secret(piece.lstrip("-"))
            and index + 1 < len(pieces)
        ):
            return True
        if "://" not in piece and _value_is_secret(piece):
            return True
    return False


def _redact_assigned_url(value: str) -> str | None:
    """名前=URL と 名前:URL の形なら、URL の部分に _redact_url を当てた値。違えば None。"""
    match = re.match(
        r"^(\s*-{0,2}[A-Za-z0-9_.\-]+\s*(?:=|:(?!//))\s*)(.*)$", value, re.DOTALL
    )
    if not match:
        return None
    prefix, rest = match.groups()
    quote = rest[:1] if rest[:1] in _QUOTES and rest[-1:] == rest[:1] else ""
    inner = rest[1:-1] if quote else rest
    url = _redact_url(inner.strip())
    if url is None:
        return None
    return f"{prefix}{quote}{url}{quote}"


def redact_arg(value: str) -> str:
    """1 つの引数から秘密を除く。秘密を含む引数は丸ごと REDACTED にし、URL は秘密の部分だけを除く。

    JSON を引数で渡す形は、秘密の名前の鍵の値と、値の形の秘密だけを除く。解析できなければ丸ごと除く。
    名前=URL の形と、値のどこかにある ://利用者:パスワード@ の形も除く。
    """
    if value.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            return REDACTED
        cleaned = _redact_json(parsed)
        return value if cleaned == parsed else json.dumps(cleaned, ensure_ascii=False)
    name = _assignment_name(value)
    if name is not None and _name_is_secret(name):
        return REDACTED
    if _pieces_hold_secret(value):
        return REDACTED
    # 値のどこにあっても、URL の利用者情報を除く。
    value = _USERINFO_RE.sub(lambda m: m.group(1) + REDACTED + "@", value)
    url = _redact_url(value.strip())
    if url is not None:
        return REDACTED if _value_is_secret(url) else url
    assigned = _redact_assigned_url(value)
    if assigned is not None:
        return assigned
    if _value_is_secret(value):
        return REDACTED
    return value


def redact_args(args: list[str]) -> tuple[list[str], int]:
    """引数の並びから秘密を除く。秘密の名前の旗の次の引数も除く。除いた数を返す。"""
    out: list[str] = []
    removed = 0
    hide_next = False
    for arg in args:
        if hide_next:
            hide_next = False
            out.append(REDACTED)
            removed += 1
            continue
        cleaned = redact_arg(arg)
        if cleaned != arg:
            removed += 1
        out.append(cleaned)
        # --token VALUE の形。値を別の引数で渡す旗は、次の引数を除く。
        if arg.startswith("-") and "=" not in arg and _name_is_secret(arg.lstrip("-")):
            hide_next = True
    return out, removed


# --- 導入物の実体 -----------------------------------------------------------------------


def _split_npm_spec(spec: str) -> tuple[str, str]:
    """npm のパッケージの指定を名前と版に分ける。@scope/name@1.0 の先頭の @ は名前の一部。"""
    head, at, version = (
        spec[1:].rpartition("@") if spec.startswith("@") else spec.rpartition("@")
    )
    if not at:
        return spec, ""
    name = ("@" + head) if spec.startswith("@") else head
    return name, version


def _split_py_spec(spec: str) -> tuple[str, str]:
    """Python のパッケージの指定 name==1.0 と name@1.0 を名前と版に分ける。extras は名前から外す。"""
    match = re.match(
        r"^([A-Za-z0-9][A-Za-z0-9._\-]*)(\[[^\]]*\])?(?:(==|@)(.+))?$", spec
    )
    if not match:
        return "", ""
    return match.group(1), (match.group(4) or "").strip()


def _positionals(
    args: list[str],
    value_flags: frozenset[str],
    bool_flags: frozenset[str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """旗と旗の値を除いた位置の引数と、値を取る旗の値。

    bool_flags を渡したとき、どちらの表にも無い旗の直後の引数は、旗の値か位置の引数かを決められない。
    そのときは位置の引数を空で返し、パッケージを推測しない。
    """
    positional: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional.extend(args[i + 1 :])
            break
        if arg.startswith("-"):
            flag, eq, inline = arg.partition("=")
            if flag in value_flags:
                if eq:
                    flags.setdefault(flag, inline)
                elif i + 1 < len(args):
                    flags.setdefault(flag, args[i + 1])
                    i += 1
            elif (
                bool_flags is not None
                and not eq
                and flag not in bool_flags
                and not positional
                and i + 1 < len(args)
                and not args[i + 1].startswith("-")
            ):
                return [], flags
            i += 1
            continue
        positional.append(arg)
        i += 1
    return positional, flags


_NPX_VALUE_FLAGS = frozenset(
    {
        "-p",
        "--package",
        "-c",
        "--call",
        "--registry",
        "--cache",
        "--userconfig",
        "--prefix",
        "-w",
        "--workspace",
        "--loglevel",
        "--node-options",
    }
)
_NPX_BOOL_FLAGS = frozenset(
    {
        "-y",
        "--yes",
        "--no",
        "-q",
        "--quiet",
        "--silent",
        "--no-install",
        "--ignore-existing",
        "--prefer-offline",
        "--prefer-online",
        "--offline",
    }
)
_UVX_VALUE_FLAGS = frozenset(
    {
        "--from",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--index",
        "--index-url",
        "--extra-index-url",
        "--default-index",
        "--refresh-package",
        "--upgrade-package",
        "--reinstall-package",
        "--index-strategy",
        "--keyring-provider",
        "--resolution",
        "--prerelease",
        "--exclude-newer",
        "--link-mode",
        "--cache-dir",
        "--directory",
        "--project",
        "--config-file",
        "--python-preference",
        "--constraints",
        "--overrides",
        "--build-constraints",
        "--env-file",
    }
)
_UVX_BOOL_FLAGS = frozenset(
    {
        "--isolated",
        "--offline",
        "--no-cache",
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "--refresh",
        "--upgrade",
        "-U",
        "--reinstall",
        "--no-progress",
        "--native-tls",
        "--no-python-downloads",
        "--no-config",
        "-n",
    }
)
_PIPX_BOOL_FLAGS = frozenset(
    {
        "--verbose",
        "-v",
        "--quiet",
        "-q",
        "--no-cache",
        "--system-site-packages",
        "--pypackages",
    }
)
_PIPX_VALUE_FLAGS = frozenset({"--spec", "--python", "--index-url", "--pip-args"})
_SCRIPT_RUNNERS = frozenset({"node", "python", "python3"})
# python の旗のうち値を取るもの。-m と -c は値がスクリプトでないため、ハッシュを取らない。
_PYTHON_VALUE_FLAGS = frozenset({"-W", "-X", "--check-hash-based-pycs"})
_UV_RUN_VALUE_FLAGS = frozenset(
    {
        "--with",
        "--directory",
        "--project",
        "--python",
        "-p",
        "--from",
        "--env-file",
        "--index",
        "--index-url",
        "--extra-index-url",
        "--cache-dir",
        "--config-file",
    }
)


def _base_name(command: str) -> str:
    """コマンドの名前。Windows の場所の区切りと .exe と .cmd の拡張子を除く。"""
    base = os.path.basename(command.replace("\\", "/")).lower()
    for ext in (".exe", ".cmd", ".bat"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def package_of(command: str, args: list[str]) -> tuple[str, str]:
    """起動の形からパッケージ名と版を取り出す。取り出せないときは空の文字列。"""
    base = _base_name(command)
    if base in ("npx", "bunx"):
        positional, flags = _positionals(args, _NPX_VALUE_FLAGS, _NPX_BOOL_FLAGS)
        spec = flags.get("--package") or flags.get("-p")
        if spec is None:
            if "-c" in flags or "--call" in flags or not positional:
                return "", ""
            spec = positional[0]
        if spec == REDACTED:
            return "", ""
        return _split_npm_spec(spec)
    if base == "uvx" or (base == "uv" and args[:2] == ["tool", "run"]):
        rest = args[2:] if base == "uv" else args
        positional, flags = _positionals(rest, _UVX_VALUE_FLAGS, _UVX_BOOL_FLAGS)
        spec = flags.get("--from") or (positional[0] if positional else "")
        return ("", "") if spec in ("", REDACTED) else _split_py_spec(spec)
    if base == "pipx" and args[:1] == ["run"]:
        positional, flags = _positionals(args[1:], _PIPX_VALUE_FLAGS, _PIPX_BOOL_FLAGS)
        spec = flags.get("--spec") or (positional[0] if positional else "")
        return ("", "") if spec in ("", REDACTED) else _split_py_spec(spec)
    return "", ""


def _local_file(value: str, cwd: str | None) -> Path | None:
    """ローカルのファイルを指す値なら、その場所。相対の場所は設定の cwd があるときだけ解く。"""
    if not value or value == REDACTED or "://" in value:
        return None
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        if not cwd:
            return None
        path = Path(os.path.expanduser(cwd)) / path
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def sha256_of(path: Path) -> str | None:
    """ファイルの sha256。上限を超えるか読めないときは None。塊で読み、上限の量を一度に確保しない。"""
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_HASH_BYTES:
                    return None
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _script_arg(args: list[str], base: str) -> str:
    """node と python が実行するスクリプトの引数。スクリプトより前の -m、-c、-e、-p は空を返す。"""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return args[i + 1] if i + 1 < len(args) else ""
        if not arg.startswith("-") or arg == "-":
            return arg if arg != "-" else ""
        if base == "node":
            if arg in ("-e", "--eval", "-p", "--print"):
                return ""
            if arg in ("-r", "--require", "--import", "--loader") and "=" not in arg:
                i += 1
        else:
            if arg.startswith(("-m", "-c")):
                return ""
            if arg in _PYTHON_VALUE_FLAGS:
                i += 1
        i += 1
    return ""


def _uv_run_script(args: list[str], cwd: str | None) -> Path | None:
    """uv run <script>.py の形のスクリプト。--directory があればその場所から解く。"""
    if "run" not in args:
        return None
    head = args[: args.index("run")]
    positional, flags = _positionals(args[args.index("run") + 1 :], _UV_RUN_VALUE_FLAGS)
    _, head_flags = _positionals(head, _UV_RUN_VALUE_FLAGS)
    if not positional or not positional[0].endswith(".py"):
        return None
    base_dir = flags.get("--directory") or head_flags.get("--directory") or cwd
    if base_dir and cwd and not os.path.isabs(os.path.expanduser(base_dir)):
        base_dir = os.path.join(cwd, base_dir)
    return _local_file(positional[0], base_dir)


def artifact_hash_of(command: str, args: list[str], cwd: str | None) -> str | None:
    """取得物のハッシュ。node と python の起動ではスクリプトの、コマンドがファイルの場所ならそのファイルの sha256。"""
    base = _base_name(command)
    target: Path | None = None
    if base in _SCRIPT_RUNNERS:
        target = _local_file(_script_arg(args, base), cwd)
    elif base == "uv":
        target = _uv_run_script(args, cwd)
    elif os.sep in command or command.startswith("~"):
        target = _local_file(command, cwd)
    return sha256_of(target) if target is not None else None


def _str_list(value: Any) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


@dataclass
class ScanItem:
    """走査と送信の 1 項目。body は受け側へ送る本文で、秘密を含まない。"""

    server: ConfiguredServer
    artifact: dict[str, Any] | None
    error: str | None = None
    display: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    scan_note: str = ""


# 製品ごとの、接続先の URL を置く項目。2 つ以上あれば接続先を決めずにエラーにする。
_URL_KEYS: dict[str, tuple[str, ...]] = {
    "gemini_cli": ("httpUrl", "url"),
    "windsurf": ("serverUrl", "url"),
}
# リモートの接続の種別の値。製品ごとの表記の揺れを全部受ける。
_REMOTE_TYPES = frozenset({"http", "sse", "streamable-http", "streamableHttp", "ws"})


def _build_isolated(
    server: ConfiguredServer,
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None]:
    """1 件の定義の想定外の失敗を、その項目のエラーにして、ほかの項目の処理を続ける。

    理由には例外のクラス名だけを載せ、定義の値を載せない。秘密を含みうるため。
    """
    try:
        return build_artifact(server)
    except Exception as exc:  # noqa: BLE001 - 1 件の想定外の失敗で他の項目を止めない
        display = {
            "product": server.product,
            "scope": server.scope,
            "path": server.path,
        }
        return None, display, f"定義を処理できない: {exc.__class__.__name__}"


def build_artifact(
    server: ConfiguredServer,
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None]:
    """設定の定義から導入物の実体の材料を作る。戻り値は (実体, 表示の補足, 作れない理由)。"""
    d = server.definition
    env = d.get("env")
    headers = d.get("headers")
    if headers is None:
        headers = d.get("http_headers")
    display: dict[str, Any] = {
        "product": server.product,
        "scope": server.scope,
        "path": server.path,
        # 値は載せず、キーの名前だけを残す。
        "env_keys": sorted(str(k) for k in env) if isinstance(env, dict) else [],
        "header_keys": sorted(str(k) for k in headers)
        if isinstance(headers, dict)
        else [],
    }
    kind = d.get("type")
    command = d.get("command")
    urls = [d.get(k) for k in _URL_KEYS.get(server.product, ("url",)) if d.get(k)]
    if len(urls) > 1:
        return None, display, "接続先の URL の項目が 2 つあり、接続先を決められない"
    url = urls[0] if urls else None
    if len(server.name) > MAX_NAME_LEN:
        return None, display, f"名前が受け側の上限 {MAX_NAME_LEN} 文字を超える"
    if kind is not None and not isinstance(kind, str):
        return None, display, "type が文字列でない"
    if url is not None and not isinstance(url, str):
        return None, display, "url が文字列でない"
    if kind in _REMOTE_TYPES or (kind is None and url and not command):
        if command:
            return None, display, "リモートの種別に command がある"
        normalized = normalize_provider_url(url) if isinstance(url, str) else None
        if not normalized:
            return None, display, "url が読めない"
        if len(normalized) > MAX_URL_LEN:
            return None, display, f"url が受け側の上限 {MAX_URL_LEN} 文字を超える"
        # パスに鍵を埋め込む URL は、置き換えると受け側の実体の鍵が変わるため、送らずにエラーにする。
        # 共通の正規化の関数はパスを残すため、ここで区切りごとに判定する。
        if REDACTED in (_redact_url(normalized) or REDACTED):
            return None, display, "url のパスに秘密に見える区切りがあるため送らない"
        return (
            {
                "kind": "mcp_server",
                "name": server.name,
                "transport": "remote",
                "url": normalized,
            },
            display,
            None,
        )
    if kind not in (None, "stdio"):
        return None, display, f"未対応の種別: {str(kind)[:64]}"
    if url:
        return None, display, "command と url の両方があり、接続の方式を決められない"
    if not isinstance(command, str) or not command.strip():
        return None, display, "command も url も無い"
    raw_args = _str_list(d.get("args"))
    if raw_args is None:
        return None, display, "args が文字列の配列でない"
    args, removed = redact_args(raw_args)
    redacted_command = redact_arg(command.strip())
    if redacted_command != command.strip():
        removed += 1
    display["redacted_args"] = removed
    if (
        len(args) > MAX_ARGS
        or any(len(a) > MAX_ARG_LEN for a in args)
        or len(redacted_command) > MAX_COMMAND_LEN
    ):
        return None, display, "コマンドか引数が受け側の上限を超える"
    cwd = d.get("cwd") if isinstance(d.get("cwd"), str) else None
    package, version = package_of(redacted_command, args)
    if len(package) > MAX_PACKAGE_LEN or len(version) > MAX_VERSION_LEN:
        return None, display, "パッケージ名か版が受け側の上限を超える"
    artifact: dict[str, Any] = {
        "kind": "mcp_server",
        "name": server.name,
        "transport": "stdio",
        "command": redacted_command,
        "args": args,
    }
    if package:
        artifact["package"] = package
    if version:
        artifact["version"] = version
    digest = artifact_hash_of(redacted_command, args, cwd)
    if digest:
        artifact["artifact_hash"] = digest
    return artifact, display, None


# --- 走査器 -------------------------------------------------------------------------------


def load_tools(path: Path) -> dict[str, Any]:
    """事前に取得した tools の JSON を、名前から定義への対応にする。

    MCP の tools/list の結果 {"tools": [...]} と、その配列だけの形を受ける。
    """
    with path.open("rb") as handle:
        raw = handle.read(MAX_TOOLS_FILE_BYTES + 1)
    if len(raw) > MAX_TOOLS_FILE_BYTES:
        raise ValueError(f"{path} が大きすぎる")
    data = json.loads(raw.decode("utf-8"))
    tools = data.get("tools") if isinstance(data, dict) else data
    if not isinstance(tools, list):
        # 呼び出し側は読み込みの失敗を ValueError で受けるため、型の誤りも同じ例外で返す。
        raise ValueError(f"{path} に tools の配列が無い")  # noqa: TRY004
    out: dict[str, Any] = {}
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or not isinstance(tool.get("name"), str)
            or not tool["name"]
        ):
            raise ValueError(f"{path} の tools に名前の無い項目がある")
        if tool["name"] in out:
            raise ValueError(f"{path} の tools に同じ名前 {tool['name']} が重なる")
        out[tool["name"]] = tool
    # 受け側の上限と同じ上限を走査の前に当てる。超えると走査しても送信で拒まれる。
    if len(out) > MAX_TOOLS:
        raise ValueError(f"{path} の tools が上限 {MAX_TOOLS} 件を超える")
    total = 0
    for name, tool in out.items():
        if len(name) > MAX_NAME_LEN:
            raise ValueError(
                f"{path} の tools の名前が上限 {MAX_NAME_LEN} 文字を超える"
            )
        size = len(json.dumps(tool, ensure_ascii=False).encode("utf-8"))
        if size > MAX_TOOL_DEFINITION_BYTES:
            raise ValueError(
                f"{path} の tools の {name} が上限 {MAX_TOOL_DEFINITION_BYTES} バイトを超える"
            )
        total += size
    if total > MAX_TOOLS_TOTAL_BYTES:
        raise ValueError(
            f"{path} の tools が合計の上限 {MAX_TOOLS_TOTAL_BYTES} バイトを超える"
        )
    return out


@dataclass
class ScanOutcome:
    report: Any = None
    unscanned_reason: str | None = None
    note: str = ""


def _run_bounded(argv: list[str], out_path: Path, timeout: float) -> int | ScanOutcome:
    """走査器を動かし、標準出力をファイルへ書かせる。時間と出力の量の上限を超えたら止める。

    出力をメモリへ読み切ってから大きさを見ると、上限の前に確保してしまうため、ファイルの大きさを見る。
    標準エラー出力は使わないため捨てる。
    """
    deadline = time.monotonic() + timeout
    with out_path.open("wb") as out:
        proc = subprocess.Popen(
            argv, stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        )
        try:
            while True:
                try:
                    returncode = proc.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if out_path.stat().st_size > MAX_SCANNER_OUTPUT:
                    return ScanOutcome(
                        unscanned_reason=REASON_ANALYZER_DISABLED,
                        note=f"{SCANNER} の出力が大きすぎる",
                    )
                if time.monotonic() >= deadline:
                    return ScanOutcome(
                        unscanned_reason=REASON_TIMEOUT,
                        note=f"{SCANNER} が {timeout:g} 秒で終わらない",
                    )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    if out_path.stat().st_size > MAX_SCANNER_OUTPUT:
        return ScanOutcome(
            unscanned_reason=REASON_ANALYZER_DISABLED,
            note=f"{SCANNER} の出力が大きすぎる",
        )
    return returncode


def run_scanner(tools: dict[str, Any], *, timeout: float) -> ScanOutcome:
    """mcp-scanner の static と YARA で tools を走査する。失敗は未走査の理由に写し、所見なしにしない。"""
    exe = shutil.which(SCANNER)
    if exe is None:
        return ScanOutcome(
            unscanned_reason=REASON_ANALYZER_DISABLED, note=f"{SCANNER} が見つからない"
        )
    with tempfile.TemporaryDirectory(prefix="scan-mcp-") as tmp:
        tools_file = Path(tmp) / "tools.json"
        tools_file.write_text(
            json.dumps({"tools": list(tools.values())}, ensure_ascii=False),
            encoding="utf-8",
        )
        # グローバルの旗はサブコマンドの前に置く。LLM と VirusTotal の解析器は指定しない。
        argv = [
            exe,
            "--analyzers",
            "yara",
            "--format",
            "raw",
            "static",
            "--tools",
            str(tools_file),
        ]
        out_path = Path(tmp) / "out.json"
        try:
            outcome = _run_bounded(argv, out_path, timeout)
        except OSError as exc:
            return ScanOutcome(
                unscanned_reason=REASON_ANALYZER_DISABLED,
                note=f"{SCANNER} を起動できない: {exc.__class__.__name__}",
            )
        if isinstance(outcome, ScanOutcome):
            return outcome
        returncode = outcome
        if returncode != 0:
            return ScanOutcome(
                unscanned_reason=REASON_ANALYZER_DISABLED,
                note=f"{SCANNER} が終了コード {returncode} で終わった",
            )
        raw = out_path.read_bytes()
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ScanOutcome(
            unscanned_reason=REASON_ANALYZER_DISABLED,
            note=f"{SCANNER} の出力が JSON として読めない",
        )
    # 形の確認は受け側が行う。形が違えば受け側が 422 を返し、失敗として表示する。
    return ScanOutcome(report=report)


# --- 本文と送信 ---------------------------------------------------------------------------


def parse_tools_options(values: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for value in values:
        # 名前は設定から来る値で、Claude Code のプロジェクトの場所のように = を含みうる。パスは利用者が
        # 選べるため、最後の = で分け、パスに = を含む指定は受けない。
        name, eq, path = value.rpartition("=")
        if not eq or not name or not path:
            raise ValueError(f"--tools-json は <名前>=<パス> の形で指定する: {value}")
        if name in out:
            raise ValueError(f"--tools-json の {name} が重なる")
        out[name] = Path(path)
    return out


def _selector_keys(server: ConfiguredServer) -> tuple[str, str, str]:
    """指定の候補。詳しい順に 製品:方式:名前、製品:名前、名前。"""
    return (
        f"{server.product}:{server.scope}:{server.name}",
        f"{server.product}:{server.name}",
        server.name,
    )


def assign_tools(
    selectors: dict[str, Path],
    servers: list[ConfiguredServer],
    artifacts: list[dict[str, Any] | None],
) -> list[Path | None]:
    """サーバごとに tools の JSON を決める。1 つの指定が実体の違う複数のサーバに当たれば止める。

    同じ名前で実体の違うサーバへ 1 つの JSON を当てると、一方の tools が他方の走査の結果になる。
    各サーバは、当たる指定のうち最も詳しいものを使う。どのサーバにも使われない指定も止める。
    """
    chosen: list[str | None] = []
    for server in servers:
        chosen.append(next((k for k in _selector_keys(server) if k in selectors), None))
    for selector in selectors:
        users = [i for i, c in enumerate(chosen) if c == selector]
        if not users:
            raise ValueError(f"--tools-json の {selector} に当たるサーバが設定に無い")
        identities = {
            json.dumps(
                {k: v for k, v in (artifacts[i] or {}).items() if k != "name"},
                sort_keys=True,
            )
            for i in users
        }
        if len(identities) > 1:
            raise ValueError(
                f"--tools-json の {selector} は実体の違う複数のサーバに当たる。"
                "<製品>:<名前> か <製品>:<方式>:<名前> で指定する"
            )
    return [selectors[c] if c is not None else None for c in chosen]


def plan_scan(
    *,
    platform: str,
    home: Path,
    system_root: Path,
    tools_options: list[str],
    timeout: float,
) -> dict[str, Any]:
    """設定を読み、走査し、送る本文を全部作る。送信はしない。"""
    read = read_configured_servers(
        platform=platform, home=home, system_root=system_root
    )
    selectors = parse_tools_options(tools_options)
    built = [_build_isolated(server) for server in read.servers]
    assigned = assign_tools(selectors, read.servers, [b[0] for b in built])
    # tools の JSON は走査器を 1 つも起動する前に全部読んで検証する。後ろのファイルが不正なら、
    # 前のファイルの走査を済ませてから失敗せず、何も走査しない。
    loaded: dict[int, dict[str, Any]] = {
        index: load_tools(tools_path)
        for index, ((artifact, _, _), tools_path) in enumerate(
            zip(built, assigned, strict=True)
        )
        if artifact is not None and tools_path is not None
    }
    items: list[ScanItem] = []
    for index, (server, (artifact, display, error), tools_path) in enumerate(
        zip(read.servers, built, assigned, strict=True)
    ):
        item = ScanItem(server=server, artifact=artifact, error=error, display=display)
        items.append(item)
        if artifact is None:
            continue
        body: dict[str, Any] = {"artifact": artifact, "scanner": SCANNER}
        if tools_path is None:
            body["unscanned_reason"] = REASON_ANALYZER_DISABLED
            item.scan_note = "tools の JSON が渡されていないため走査していない"
        else:
            tools = loaded[index]
            outcome = run_scanner(tools, timeout=timeout)
            if outcome.unscanned_reason is not None:
                body["unscanned_reason"] = outcome.unscanned_reason
                item.scan_note = outcome.note
            else:
                body["report"] = outcome.report
                body["tools"] = tools
        item.body = body
    return {"read": read, "items": items}


def send_body(
    endpoint: str, api_key: str, body: dict[str, Any], *, timeout: float
) -> tuple[int, Any]:
    """本文を 1 件送る。応答の状態と本文を返す。通信の失敗は状態 0。鍵は表示に載せない。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    headers.update(sign_headers(data))
    req = urllib.request.Request(
        endpoint.rstrip("/") + SCAN_PATH, data=data, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            raw = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(MAX_RESPONSE_BYTES) if exc.fp is not None else b""
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": f"送信できない: {exc.__class__.__name__}"}
    try:
        payload: Any = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"error": "応答が JSON でない"}
    return status, payload


def run(args: Any) -> int:
    """scan-mcp の入口。--send が無いか --dry-run なら、送る内容を表示するだけにする。"""
    from senda_argus_hooks.cli.agent_hooks_install import current_platform

    send = bool(args.send) and not args.dry_run
    endpoint = ""
    api_key = ""
    timeout_send = 10.0
    if send:
        # 送出の設定は既存の Argus の送出と同じ環境変数から読む。
        endpoint = os.environ.get("SENDA_ARGUS_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError("SENDA_ARGUS_ENDPOINT is required for --send")
        api_key = os.environ.get("SENDA_ARGUS_API_KEY", "").strip()
        timeout_send = float(int(os.environ.get("SENDA_ARGUS_TIMEOUT", "10") or 10))
    plan = plan_scan(
        platform=args.platform or current_platform(),
        home=Path(args.home) if args.home else Path.home(),
        system_root=Path(args.system_root) if args.system_root else Path("/"),
        tools_options=list(args.tools_json or []),
        timeout=float(args.timeout),
    )
    read = plan["read"]
    rows: list[dict[str, Any]] = []
    failed = 0
    for item in plan["items"]:
        row: dict[str, Any] = {"server": item.server.name, **item.display}
        if item.body is None:
            row["error"] = item.error
            failed += 1
            rows.append(row)
            continue
        row["body"] = item.body
        if item.scan_note:
            row["scan_note"] = item.scan_note
        if send:
            status, payload = send_body(
                endpoint, api_key, item.body, timeout=timeout_send
            )
            row["response_status"] = status
            row["response"] = payload
            if status != 200:
                failed += 1
        rows.append(row)
    summary = {
        "sent": send,
        "servers_read": len(read.servers),
        "unreadable_products": read.unreadable_products,
        "refused_products": read.refused_products,
        "skipped_servers": read.skipped_servers,
        "items": rows,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # 読み取りの失敗は終了コードに出す。読まないと決めた置き場所 (refused_products) は出さない。
    return 1 if failed or read.skipped_servers or read.unreadable_products else 0
