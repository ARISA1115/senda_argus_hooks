"""tool の呼び出しが監視の構成要素を書き換えるかを、引数から導く。

監視を黙らせる最短の経路は、監視の構成要素を書き換えることである。収集 hook の設定と環境変数、
Argus への接続先と鍵、規則と閾値の定義、承認済み tool の基準線、通知の送出先。判定はここが返す
区分の並びだけを読み、引数の形を知らない。

**導出の規則は収集 hook と同じにする。** 収集 hook は引数の本文を送らない既定の構成でも、この
区分の並びだけを送る。受け取り側は本文が届いた経路で同じ規則を当てる。この写しは受け取り側の実装と一字一句同じ規則にし、値の一致は受け取り側の検査で固定する。規則が 2 つに分かれると、
片方の経路でだけ同じ書き換えが見えなくなる。値の一致は検査で固定する。

**読むだけの呼び出しは返さない。** 設定を読むのは正常な運用に含まれる。ただし読み取りと言い切れる
ものだけを外す。本文にあたる引数が無いことは読み取りを意味しない。消す操作も権限を変える操作も
本文を持たない。読み取りと言い切れるのは、名前が読み取りの動詞だけで書き込みの動詞を含まず、本文も
命令も持たない呼び出しと、命令が読み取りの命令だけでできている呼び出しである。**判別できない
呼び出しは書き換えとして扱う。** 判別できないものを読み取りへ倒すと、名前の無い tool で書くだけで
検知を避けられる。

**パスは正規化してから照合する。** 区切りの向き、重複した区切り、``.`` と ``..``、パーセント符号化、
大文字と小文字、引用符で割った綴り、ワイルドカードを揃える。揃えないと、綴りを変えるだけで同じ
ファイルを書き換えられる。
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
from typing import Any, Final
from urllib.parse import unquote, urlsplit

# 区分の名前。判定と表示はこの名前で扱う。
COLLECTOR_CONFIG: Final[str] = "collector_config"
ARGUS_CONNECTION: Final[str] = "argus_connection"
DETECTION_CONFIG: Final[str] = "detection_config"
DETECTION_RULES: Final[str] = "detection_rules"
TOOL_BASELINE: Final[str] = "tool_baseline"
NOTIFICATION: Final[str] = "notification"
OPERATOR: Final[str] = "operator"
# 走査の上限で照合しきれなかった区分。照合しきれなかった部分に対象が在りうるため、判別不能として扱う。
UNSCANNED: Final[str] = "unscanned"

CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        COLLECTOR_CONFIG,
        ARGUS_CONNECTION,
        DETECTION_CONFIG,
        DETECTION_RULES,
        TOOL_BASELINE,
        NOTIFICATION,
        OPERATOR,
        UNSCANNED,
    }
)

# 運用者が対象を足す環境変数。値はパスの末尾をカンマで並べたもの。
OPERATOR_PATHS_ENV: Final[str] = "SENDA_ARGUS_MONITOR_PATHS"

# 収集 hook と収集器と Argus が読む環境変数の名前。接頭辞と、接頭辞を持たない個別の名前。
_ENV_RE: Final[Any] = re.compile(
    r"(?i)(?<![A-Za-z0-9_])("
    r"(?:SENDA_ARGUS|ARGUS|MCP_AUDIT_LOG)_[A-Za-z0-9_]+"
    r"|MCP_AGENT_ALLOWLIST_PATH|MCP_CPE_MAP_PATH|MCP_DEFAULT_AGENT_ID"
    r")(?![A-Za-z0-9_])"
)

# 接続先と鍵を表す語。環境変数の名前にこれを含めば接続の区分とする。
_CONNECTION_WORDS: Final[tuple[str, ...]] = ("ENDPOINT", "API_KEY", "SECRET", "TOKEN")

# ファイルの名前。基底名で照合する。ワイルドカードの綴りもこの名前に対して照合する。
_FILE_NAMES: Final[dict[str, str]] = {
    "hooks.env": COLLECTOR_CONFIG,
    "senda_argus_autohook.pth": COLLECTOR_CONFIG,
    "token_price.yaml": DETECTION_RULES,
    "cpe_map.json": DETECTION_RULES,
    "agent_allowlist.json": TOOL_BASELINE,
    "agent_allowlist.dev.json": TOOL_BASELINE,
}

# ファイルの名前のうち、置き場所を問わずに対象とするもの。hooks.env のようなよくある名前は
# 置き場所と合わせて照合する。
_UNIQUE_FILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "senda_argus_autohook.pth",
        "token_price.yaml",
        "cpe_map.json",
        "agent_allowlist.json",
        "agent_allowlist.dev.json",
    }
)

# パスに含まれる区間。区切りで囲んで照合する。
_PATH_SEGMENTS: Final[tuple[tuple[str, str], ...]] = (
    ("/etc/senda-argus/", COLLECTOR_CONFIG),
    ("/.config/senda-argus/", COLLECTOR_CONFIG),
    ("/senda-argus/hooks.env/", COLLECTOR_CONFIG),
    ("/senda_argus_hooks/", COLLECTOR_CONFIG),
    ("/argus_collector/", COLLECTOR_CONFIG),
    ("/detection_core/infrastructure/rules/", DETECTION_RULES),
    ("/tool-allowlist/", TOOL_BASELINE),
    ("/etc/argus/", TOOL_BASELINE),
)

# Argus の設定を変える API の経路。接頭辞で照合する。
_API_PATHS: Final[tuple[tuple[str, str], ...]] = (
    ("/v1/rules", DETECTION_RULES),
    ("/v1/threat-feeds", DETECTION_RULES),
    ("/v1/admin/mcp-inventory", TOOL_BASELINE),
    ("/v1/integrations", NOTIFICATION),
    ("/v1/admin/governance", DETECTION_CONFIG),
    ("/v1/admin/api-keys", ARGUS_CONNECTION),
)

# 本文にあたる引数の名前。これが在れば書き込みとする。値が空でも書き込みとする。
_BODY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "content",
        "text",
        "body",
        "data",
        "new_string",
        "new_str",
        "contents",
        "value",
        "file_text",
        "source",
        "patch",
        "diff",
        "payload",
        "json",
        "rows",
        "records",
    }
)

# 命令にあたる引数の名前。
_COMMAND_KEYS: Final[frozenset[str]] = frozenset(
    {"command", "cmd", "script", "code", "shell", "commandline", "command_line"}
)

# HTTP の方式を運ぶ引数の名前と、読み取りの方式。
_METHOD_KEYS: Final[frozenset[str]] = frozenset({"method", "http_method"})
_READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

# tool の名前の語。読み取りの語だけを含み書き込みの語を含まないとき、読み取りとする。
_READ_WORDS: Final[frozenset[str]] = frozenset(
    {
        "read",
        "get",
        "view",
        "list",
        "show",
        "cat",
        "head",
        "tail",
        "search",
        "grep",
        "find",
        "stat",
        "describe",
        "inspect",
        "fetch",
        "query",
        "lookup",
    }
)
_WRITE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "write",
        "edit",
        "delete",
        "del",
        "remove",
        "rm",
        "set",
        "unset",
        "update",
        "put",
        "patch",
        "post",
        "move",
        "mv",
        "rename",
        "create",
        "append",
        "exec",
        "execute",
        "run",
        "shell",
        "bash",
        "command",
        "chmod",
        "chown",
        "truncate",
        "replace",
        "save",
        "upload",
        "kill",
        "stop",
        "disable",
        "enable",
        "modify",
        "insert",
        "drop",
        "copy",
        "cp",
        "install",
        "uninstall",
        "configure",
    }
)

# 読み取りだけをする命令。命令の各区間がこのいずれかで始まり、書き出しを伴わないとき読み取りとする。
_READ_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "cat",
        "less",
        "more",
        "head",
        "tail",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ls",
        "stat",
        "file",
        "wc",
        "printenv",
        "echo",
        "md5sum",
        "sha256sum",
        "get-content",
        "get-item",
        "get-childitem",
        "type",
        "dir",
    }
)

# 命令を区間に分ける記号と、書き出しや入れ子の実行を表す記号。
_SEGMENT_SPLIT: Final[Any] = re.compile(r"\|\||&&|[;|&\n]")
_COMMAND_WRITE_MARKS: Final[tuple[str, ...]] = (">", "$(", "`", "<(")

# 語へ分ける区切り。シェルの区切りと代入の記号と括弧で切る。
_TOKEN_SPLIT: Final[Any] = re.compile(r"[\s;|&<>()=,{}]+")

# 走査する文字列の数の上限。入れ子の深い引数で処理が伸びないようにする。
MAX_SCANNED_STRINGS: Final[int] = 4096

# 照合する文字列の長さの上限。超えた分は先頭と末尾を残して照合する。
MAX_STRING_LEN: Final[int] = 65536


def _strings(
    value: Any,
    out: list[tuple[str, str, bool]],
    key: str,
    budgets: dict[bool, list[int]],
    in_body: bool = False,
) -> bool:
    """引数を辿り、(引数の名前, 文字列, 本文の内側か) を集める。

    本文の外で上限に達したら False を返す。本文の内側は別の上限で数え、打ち切っても False に
    しない。本文は書き込む中身であり、宛先は本文の外の引数が指す。本文の大きさで打ち切りの印を
    立てると、大きなファイルの書き込みや行の一括の挿入が全部、監視の書き換えに見える。
    """
    budget = budgets[in_body]
    if budget[0] <= 0:
        return in_body
    if isinstance(value, str):
        budget[0] -= 1
        out.append((key, value, in_body))
    elif isinstance(value, dict):
        for k, v in value.items():
            child_body = in_body
            if isinstance(k, str):
                if budget[0] <= 0:
                    return in_body
                budget[0] -= 1
                out.append(("", k, in_body))
                child_body = in_body or k.lower() in _BODY_KEYS
            if not _strings(v, out, k if isinstance(k, str) else "", budgets, child_body):
                return False
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not _strings(item, out, key, budgets, in_body):
                return False
    return True


def _clip(text: str) -> str:
    if len(text) <= MAX_STRING_LEN:
        return text
    half = MAX_STRING_LEN // 2
    return text[:half] + "\n" + text[-half:]


def _path_forms(token: str) -> list[str]:
    """語をパスとして正規化した形を返す。バックスラッシュは区切りとエスケープの 2 通りで読む。"""
    text = token.strip().strip("\"'")
    if not text:
        return []
    if text.lower().startswith("file://"):
        text = text[len("file://") :]
    text = unquote(text)
    # 引用符で割った綴りを戻す。シェルでは hoo''ks.env も hooks.env を指す。
    text = text.replace("'", "").replace('"', "")
    forms: list[str] = []
    for variant in (text.replace("\\", "/"), text.replace("\\", "")):
        collapsed = re.sub(r"/+", "/", variant)
        if not collapsed:
            continue
        normalized = posixpath.normpath(collapsed)
        if collapsed.endswith("/") and not normalized.endswith("/"):
            normalized += "/"
        lowered = normalized.lower()
        if lowered not in forms:
            forms.append(lowered)
    return forms


def _operator_paths() -> list[str]:
    raw = os.environ.get(OPERATOR_PATHS_ENV, "")
    out: list[str] = []
    for item in raw.split(","):
        for form in _path_forms(item):
            form = form.strip("/")
            if form and form != "." and form not in out:
                out.append(form)
    return out


def _path_categories(token: str, operator: list[str]) -> set[str]:
    found: set[str] = set()
    for form in _path_forms(token):
        bounded = "/" + form.strip("/") + "/"
        base = posixpath.basename(form.rstrip("/"))
        for segment, category in _PATH_SEGMENTS:
            if segment in bounded:
                found.add(category)
        if base in _UNIQUE_FILE_NAMES:
            found.add(_FILE_NAMES[base])
        if any(ch in base for ch in "*?["):
            # ワイルドカードの綴りは、既知の名前に当てはまるかで見る。
            for name, category in _FILE_NAMES.items():
                if name in _UNIQUE_FILE_NAMES and fnmatch.fnmatchcase(name, base):
                    found.add(category)
        if base.startswith("agent_allowlist") and base.endswith(".json"):
            found.add(TOOL_BASELINE)
        for entry in operator:
            if bounded.endswith("/" + entry + "/") or ("/" + entry + "/") in bounded:
                found.add(OPERATOR)
    return found


def _env_category(name: str) -> str:
    upper = name.upper()
    if any(word in upper for word in _CONNECTION_WORDS):
        return ARGUS_CONNECTION
    if "ALLOWLIST" in upper:
        return TOOL_BASELINE
    if upper == "MCP_CPE_MAP_PATH":
        return DETECTION_RULES
    if upper.startswith("MCP_AUDIT_LOG_"):
        return DETECTION_CONFIG
    return COLLECTOR_CONFIG


def _api_categories(text: str) -> set[str]:
    found: set[str] = set()
    for match in re.findall(r"(?i)\bhttps?://[^\s\"'<>`]+", text):
        try:
            path = urlsplit(match).path
        except ValueError:
            continue
        forms = _path_forms(path)
        for form in forms:
            for prefix, category in _API_PATHS:
                if form == prefix or form.startswith(prefix + "/"):
                    found.add(category)
    return found


def _categories_in(text: str, operator: list[str]) -> set[str]:
    text = _clip(text)
    found: set[str] = set()
    for match in _ENV_RE.findall(text):
        found.add(_env_category(match))
    found |= _api_categories(text)
    for token in _TOKEN_SPLIT.split(text):
        if token:
            found |= _path_categories(token, operator)
    return found


def _name_words(tool: Any) -> set[str]:
    if not isinstance(tool, str):
        return set()
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tool)
    return {w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w}


def _command_is_read_only(command: str) -> bool:
    if any(mark in command for mark in _COMMAND_WRITE_MARKS):
        return False
    segments = [s.strip() for s in _SEGMENT_SPLIT.split(command)]
    segments = [s for s in segments if s]
    if not segments:
        return False
    for segment in segments:
        head = segment.split()[0].strip("\"'")
        name = posixpath.basename(head.replace("\\", "/")).lower()
        if name not in _READ_COMMANDS:
            return False
    return True


def _command_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return " ".join(value)
    return None


def _mutation_marks(value: Any, commands: list[Any], budget: list[int]) -> bool:
    """入れ子を辿り、変更を示す印があるか、上限で打ち切ったら True を返す。命令は commands へ集める。

    対象の探索と同じ深さまで辿る。最上段だけを見ると、入れ子の要求に置いた破壊的な方式や本文が
    読み取りに見える。
    """
    if budget[0] <= 0:
        return True
    budget[0] -= 1
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                lowered = k.lower()
                if lowered in _BODY_KEYS:
                    return True
                if lowered in _METHOD_KEYS and (
                    not isinstance(v, str) or v.strip().upper() not in _READ_METHODS
                ):
                    return True
                if lowered in _COMMAND_KEYS:
                    commands.append(v)
            if _mutation_marks(v, commands, budget):
                return True
    elif isinstance(value, (list, tuple)):
        for item in value:
            if _mutation_marks(item, commands, budget):
                return True
    return False


def _is_read_only(arguments: Any, tool: Any) -> bool:
    """読み取りと言い切れる呼び出しか。言い切れなければ False を返す。"""
    commands: list[Any] = []
    if _mutation_marks(arguments, commands, [MAX_SCANNED_STRINGS]):
        return False
    if commands:
        texts = [_command_text(value) for value in commands]
        if any(t is None for t in texts):
            return False
        return all(_command_is_read_only(t) for t in texts if t is not None)
    words = _name_words(tool)
    if words & _WRITE_WORDS:
        return False
    return bool(words & _READ_WORDS)


def monitor_targets(arguments: Any, *, tool: Any = None) -> list[str]:
    """呼び出しが書き換える監視の構成要素の区分を返す。該当しないか読むだけなら空を返す。

    本文の外の引数を走査の上限で打ち切ったか、本文の外の長い文字列の中ほどを照合から外したときは、
    照合できなかった部分に対象が在りうる。読み取りと言い切れない呼び出しなら UNSCANNED を足す。足さないと、詰め物で
    対象のパスを上限の外へ押し出すだけで検知を避けられる。読み取りの判定も同じ上限で打ち切るため、
    打ち切った呼び出しは名前が読み取りでも読み取りと言い切らない。打ち切った先に本文や方式が在りうる。
    """
    pairs: list[tuple[str, str, bool]] = []
    complete = _strings(
        arguments,
        pairs,
        "",
        {False: [MAX_SCANNED_STRINGS], True: [MAX_SCANNED_STRINGS]},
    )
    if not pairs and complete:
        return []
    operator = _operator_paths()
    found: set[str] = set()
    for _key, value, in_body in pairs:
        if len(value) > MAX_STRING_LEN and not in_body:
            complete = False
        found |= _categories_in(value, operator)
    if not complete:
        found.add(UNSCANNED)
    if not found:
        return []
    if _is_read_only(arguments, tool):
        return []
    return sorted(found)
