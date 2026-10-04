"""tool の呼び出しの引数に現れる、依存の導入と資源の取得を取り出す。

言語モデルが作り出した実在しない名前を攻撃者が先に公開先へ登録し、エージェントに取得させる手口が
ある。取得先のホストは正規の公開先のままなので、宛先のホストの初出では捉えられない。受け取り側は
主体ごとに取得先の基準線を持ち、基準線の確立の後に初めて現れた取得先で発火する。ここでは引数から
取得先を取り出す。判定は受け取り側が行う。

**導出の規則は受け取り側と同じにする。** 引数の本文を送らない既定の構成では、ここで導いた値だけを
送る。受け取り側は本文が届いた経路で同じ規則を当てる。規則が分かれると、同じ取得先が経路ごとに
違う名前になり、基準線の突合が静かに崩れる。値の一致は受け取り側の検査で固定する。

**取得先は「種別:名前」で表す。** 版は取得先に含めない。既知の依存の版を上げることは初出ではない。
版と、指定されていればダイジェストは、記録のために別に持つ。

**名前は種別ごとの規則で畳む。** 大文字と小文字、区切りの揺れ、利用者の情報やポートを揃える。
揃えないと、同じ取得先を綴り替えるだけで初出に見せられる。長い名前は捨てずにダイジェストへ畳む。
捨てると、長い名前を選ぶだけでその取得が判定から外れる。

**名前として通す字を種別ごとに限る。** 外れる語は名前として扱わず、判別できない取得とする。字を
1 つずつ禁じる形にすると、禁じ忘れた区切りの字を挟むだけで既知の名前に見せられる。

**上限で打ち切ったことを返す。** 走査する文字列の数と深さ、1 件の呼び出しから出す取得先の数、
入れ子のシェルの段数に上限を置く。上限で打ち切ったときと、取得を表す手順なのに取得先を判別でき
ないときは印を返し、受け取り側は判別できない取得として検知する側へ倒す。既知の取得先を先に並べて
本命を上限の外へ押し出す手を通さない。

**導入の手順の外の語は拾わない。** 取得を表すのは、導入の道具とその動詞の後に続く語だけである。
文の中の語をパッケージ名として拾うと、無関係な呼び出しが初出として発火する。
"""

from __future__ import annotations

import hashlib
import re
import shlex
from typing import Any, Final
from urllib.parse import urlsplit

# 1 件の呼び出しから出す取得先の数の上限。
MAX_ACQUISITIONS_PER_CALL: Final[int] = 32
# 走査する文字列の数と入れ子の深さの上限。
MAX_SCANNED_STRINGS: Final[int] = 4096
MAX_SCAN_DEPTH: Final[int] = 32
# 1 つの文字列から読む長さの上限。超えた分は読まず、打ち切りの印を立てる。
MAX_COMMAND_CHARS: Final[int] = 65536
# sh -c の中の sh -c を辿る段数の上限。
MAX_SHELL_NESTING: Final[int] = 4
# 名前の長さの上限。超える名前はダイジェストへ畳む。
MAX_NAME_LEN: Final[int] = 214

PYPI: Final[str] = "pypi"
NPM: Final[str] = "npm"
CRATES: Final[str] = "crates"
GO: Final[str] = "go"
GEM: Final[str] = "gem"
GIT: Final[str] = "git"
HF: Final[str] = "hf"
URL: Final[str] = "url"
# 依存の一覧のファイルからの導入。中身は見えないため、ファイルを取得先として扱う。
MANIFEST: Final[str] = "manifest"
# 既定と異なる取得の元。名前の解決先が変わるため、それ自体を取得先として扱う。
INDEX: Final[str] = "index"

# コマンドの前に置かれ、後ろの語を実行するだけのものと、その値を取るオプション。
_WRAPPERS: Final[dict[str, frozenset[str]]] = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "time": frozenset({"-f", "-o"}),
    "nohup": frozenset(),
    "nice": frozenset({"-n"}),
}
# 取得の元を差し替える環境変数と、その種別。オプションの代わりに代入で渡しても同じ取得先にする。
_INDEX_VARIABLES: Final[dict[str, str]] = {
    "PIP_INDEX_URL": PYPI,
    "PIP_EXTRA_INDEX_URL": PYPI,
    "PIP_FIND_LINKS": PYPI,
    "UV_INDEX_URL": PYPI,
    "UV_EXTRA_INDEX_URL": PYPI,
    "UV_DEFAULT_INDEX": PYPI,
    "UV_INDEX": PYPI,
    "NPM_CONFIG_REGISTRY": NPM,
    "YARN_REGISTRY": NPM,
    "CARGO_REGISTRIES_CRATES_IO_INDEX": CRATES,
    "GOPROXY": GO,
}
# 導入の道具の名前と、取得を表す動詞。語の並びにこの組が現れたのに道具まで辿れなかったときは、
# 判別できない取得として印を立てる。前に置く語を 1 つずつ一覧へ足す形にすると、足し忘れた語を
# 前に置くだけで取得が黙って消える。
_INSTALL_VERBS: Final[dict[str, frozenset[str]]] = {
    "pip": frozenset({"install", "download", "wheel"}),
    "uv": frozenset({"pip", "add", "tool", "run"}),
    "uvx": frozenset(),
    "pipx": frozenset({"install", "run", "inject"}),
    "poetry": frozenset({"add"}),
    "pdm": frozenset({"add"}),
    "rye": frozenset({"add"}),
    "hatch": frozenset({"add"}),
    "npm": frozenset({"install", "i", "add", "ci", "exec", "x", "create", "init"}),
    "pnpm": frozenset({"install", "i", "add", "dlx", "exec", "create"}),
    "yarn": frozenset({"add", "install", "dlx", "create"}),
    "bun": frozenset({"add", "install", "i", "x", "create"}),
    "cnpm": frozenset({"install", "i", "add"}),
    "npx": frozenset(),
    "bunx": frozenset(),
    "pnpx": frozenset(),
    "cargo": frozenset({"install", "add"}),
    "go": frozenset({"get", "install"}),
    "gem": frozenset({"install"}),
    "git": frozenset({"clone"}),
    "huggingface-cli": frozenset({"download"}),
    "hf": frozenset({"download"}),
}
# 道具の動詞の前に置ける、値を取る共通のオプション。値ごと読み飛ばしてから動詞を読む。
_GLOBAL_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config", "--directory",
        "--project", "--python", "-p", "--prefix", "--cache-dir", "--log", "--proxy",
        "--cwd", "--userconfig", "--registry", "-Z", "--color", "--manifest-path",
        "--timeout", "--retries", "--cert", "--client-cert", "--exists-action",
        "--dir", "--filter",
    }
)
# 後ろに続く文字列をシェルとして実行するもの。
_SHELLS: Final[frozenset[str]] = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})

# 値を取るオプション。値をパッケージ名として拾わないように読み飛ばす。
_PIP_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "-t", "--target", "--prefix", "--root", "--python", "--platform",
        "--python-version", "--implementation", "--abi", "--src", "--cache-dir",
        "--log", "--proxy", "--retries", "--timeout", "--exists-action",
        "--trusted-host", "--cert", "--client-cert", "--upgrade-strategy",
        "--progress-bar", "--root-user-action", "--report", "--config-settings",
        "-C", "--global-option", "--no-binary", "--only-binary", "--python-executable",
        "-p", "--group", "--extra", "--optional", "--package", "-d", "--dest",
    }
)
_PIP_INDEX_OPTIONS: Final[frozenset[str]] = frozenset(
    {"-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--index", "--default-index"}
)
_PIP_MANIFEST_OPTIONS: Final[frozenset[str]] = frozenset(
    {"-r", "--requirement", "-c", "--constraint", "--requirements", "--constraints"}
)
_PIP_EDITABLE_OPTIONS: Final[frozenset[str]] = frozenset({"-e", "--editable"})
_UVX_FROM_OPTIONS: Final[frozenset[str]] = frozenset({"--from", "--spec"})
_UVX_WITH_OPTIONS: Final[frozenset[str]] = frozenset({"--with", "-w"})
_NPM_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--prefix", "-C", "--dir", "--cwd", "--workspace", "-w", "--tag", "--cache", "--userconfig", "--filter"}
)
_NPM_INDEX_OPTIONS: Final[frozenset[str]] = frozenset({"--registry"})
_NPM_PACKAGE_OPTIONS: Final[frozenset[str]] = frozenset({"-p", "--package"})
_NPM_INSTALL_VERBS: Final[frozenset[str]] = frozenset(
    {"install", "i", "add", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal", "isntall"}
)
_NPM_EXEC_VERBS: Final[frozenset[str]] = frozenset({"exec", "x", "dlx"})
_CARGO_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--version", "--vers", "--root", "--branch", "--tag", "--rev", "--path", "--features",
     "-F", "--target", "--profile", "-j", "--jobs", "--package", "-p", "--rename"}
)
_CARGO_SOURCE_OPTIONS: Final[frozenset[str]] = frozenset({"--git", "--index", "--registry"})
_GO_VALUE_OPTIONS: Final[frozenset[str]] = frozenset({"-modfile", "-C", "-o", "-tags"})
_GEM_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"-v", "--version", "-i", "--install-dir", "-n", "--bindir", "--platform"}
)
_GEM_INDEX_OPTIONS: Final[frozenset[str]] = frozenset({"-s", "--source"})
_GIT_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"-b", "--branch", "-o", "--origin", "--depth", "--reference", "-c", "--config",
     "--template", "-u", "--upload-pack", "--separate-git-dir", "--shallow-since",
     "--shallow-exclude", "-j", "--jobs", "--filter"}
)
_HF_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--repo-type", "--revision", "--include", "--exclude", "--local-dir", "--cache-dir",
     "--token", "--max-workers"}
)

# 名前として通す綴り。種別ごとに許す字を限る。
_PYPI_NAME: Final[Any] = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# 名前の後ろに続いてよい、追加の機能と版の指定。
_PYPI_REST: Final[Any] = re.compile(
    r"\s*(?:\[[A-Za-z0-9,._\- ]*\])?\s*"
    r"(?:(===|==|~=|!=|<=|>=|<|>)\s*([A-Za-z0-9.*+!_\-]+)"
    r"(?:\s*,\s*(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_\-]+)*)?\s*"
)
_NPM_NAME: Final[Any] = re.compile(r"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*")
_NPM_VERSION: Final[Any] = re.compile(r"[a-z0-9.^~<>=|*+_\- ]*")
_GO_PATH: Final[Any] = re.compile(r"[a-z0-9][a-z0-9._~/-]*")
_PLAIN_NAME: Final[Any] = re.compile(r"[a-z0-9][a-z0-9._-]*")
_PLAIN_VERSION: Final[Any] = re.compile(r"[a-z0-9.+_\-]*")
_HF_REPO: Final[Any] = re.compile(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?")
_HASH_VALUE: Final[Any] = re.compile(r"[a-z0-9]{2,16}:[0-9a-f]{32,128}")
_ASSIGNMENT: Final[Any] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_PYTHON: Final[Any] = re.compile(r"python(?:\d+(?:\.\d+)?)?|py")
_PIP: Final[Any] = re.compile(r"pip(?:\d+(?:\.\d+)?)?")
_GITHUB_SHORTHAND: Final[Any] = re.compile(r"(?:github:)?([a-z0-9][a-z0-9-]*)/([a-z0-9._-]+)")
_SCP_LIKE: Final[Any] = re.compile(r"(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+):([^\s]+)")

# シェルの区切りの字。区切りの前後で別のコマンドとして読む。
_PUNCTUATION: Final[str] = ";&|()"


def _fold(name: str) -> str:
    """長い名前と、空白や制御の字を含む名前をダイジェストへ畳む。

    捨てずに、同じ名前は同じ値へ、違う名前は違う値へ写す。空白や制御の字を残すと、受け取り側が
    並びの形を確かめるときに崩れた並びとして扱われ、取得先が判別できない取得に化ける。
    """
    if len(name) <= MAX_NAME_LEN and all(c.isprintable() and not c.isspace() for c in name):
        return name
    return "#" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]


def normalize_location(raw: str) -> str | None:
    """URL か scp 形式の場所を「ホスト/経路」へ畳む。場所として読めなければ None を返す。

    資格情報とポートと問い合わせと断片と末尾の .git を落とし、大小文字を揃える。経路の最後の段に
    付いた @ の後ろは版の指定なので落とす。段の先頭の @ は範囲の印なので残す。
    """
    text = raw.strip()
    for prefix in ("git+", "hg+", "svn+", "bzr+"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    if "://" in text:
        try:
            parts = urlsplit(text)
            host = parts.hostname or ""
        except ValueError:
            return None
        path = parts.path
    else:
        scp = _SCP_LIKE.fullmatch(text)
        if scp is None:
            return None
        host, path = scp.group(1), scp.group(2).split("#", 1)[0]
    host = host.strip().lower().rstrip(".")
    if not host or "." not in host and host != "localhost":
        return None
    segments = [s for s in path.lower().split("/") if s]
    if segments and "@" in segments[-1][1:]:
        last = segments[-1]
        segments[-1] = last[0] + last[1:].split("@", 1)[0]
    if segments and segments[-1].endswith(".git"):
        segments[-1] = segments[-1][: -len(".git")]
    segments = [s for s in segments if s]
    return _fold("/".join([host, *segments]))


def _item(kind: str, name: str, version: str | None = None, digest: str | None = None) -> dict[str, str]:
    name = _fold(name)
    out = {"ecosystem": kind, "name": name, "source": f"{kind}:{name}"}
    if version:
        out["version"] = version.strip()[:MAX_NAME_LEN]
    if digest:
        out["hash"] = digest
    return out


class _Collector:
    __slots__ = ("items", "seen", "truncated")

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []
        self.seen: set[str] = set()
        self.truncated = False

    def add(self, item: dict[str, str]) -> None:
        if item["source"] in self.seen:
            return
        if len(self.items) >= MAX_ACQUISITIONS_PER_CALL:
            self.truncated = True
            return
        self.seen.add(item["source"])
        self.items.append(item)

    def undetermined(self) -> None:
        """取得を表しているが取得先を判別できない語。印を立て、検知する側へ倒す。"""
        self.truncated = True


def _is_location(token: str) -> bool:
    lowered = token.lower()
    return "://" in lowered or lowered.startswith(("git@", "git+"))


def _add_location(token: str, out: _Collector) -> None:
    location = normalize_location(token)
    if location is None:
        out.undetermined()
        return
    lowered = token.lower()
    is_git = lowered.startswith(("git+", "git@", "git://", "ssh://")) or lowered.split("#", 1)[0].split("?", 1)[0].rstrip("/").endswith(".git")
    out.add(_item(GIT if is_git else URL, location))


def _is_local(token: str) -> bool:
    """手元のファイルや入れ物を指す語。取得ではないため数えない。"""
    lowered = token.lower()
    return (
        lowered.startswith((".", "/", "~", "file:"))
        or "\\" in token
        or lowered.endswith((".whl", ".tar.gz", ".zip", ".tgz", ".gem", ".crate", ".tar.bz2"))
    )


def _pypi_spec(token: str, out: _Collector, digest: str | None = None) -> None:
    spec = token.split(";", 1)[0].strip()
    if not spec:
        return
    if " @ " in spec or ("@" in spec and "://" in spec.split("@", 1)[1]):
        # 名前 @ URL の指定。URL を取得先とする。
        _add_location(spec.split("@", 1)[1].strip(), out)
        return
    if _is_location(spec):
        _add_location(spec, out)
        return
    if _is_local(spec):
        return
    match = _PYPI_NAME.match(spec)
    rest = _PYPI_REST.fullmatch(spec, match.end()) if match is not None else None
    if match is None or rest is None:
        out.undetermined()
        return
    version = rest.group(2) if rest.group(1) in ("==", "===") else None
    out.add(_item(PYPI, re.sub(r"[-_.]+", "-", match.group(0)).lower(), version, digest))


def _npm_spec(token: str, out: _Collector) -> None:
    if _is_location(token):
        _add_location(token, out)
        return
    if _is_local(token):
        return
    lowered = token.strip().lower()
    lowered = lowered.removeprefix("npm:")
    if not lowered.startswith("@"):
        short = _GITHUB_SHORTHAND.fullmatch(lowered.split("#", 1)[0])
        if short is not None:
            repo = short.group(2).removesuffix(".git")
            out.add(_item(GIT, f"github.com/{short.group(1)}/{repo}"))
            return
    # 名前と版は最後の @ で分ける。先頭の @ は範囲の印である。
    at = lowered.rfind("@")
    name, version = (lowered[:at], lowered[at + 1:]) if at > 0 else (lowered, "")
    if not _NPM_NAME.fullmatch(name) or not _NPM_VERSION.fullmatch(version):
        out.undetermined()
        return
    out.add(_item(NPM, name, version or None))


def _plain_spec(kind: str, token: str, out: _Collector, sep: str = "@") -> None:
    if _is_location(token):
        _add_location(token, out)
        return
    if _is_local(token):
        return
    name, _, version = token.strip().lower().partition(sep)
    if kind == GO:
        name = name.removesuffix("/...").rstrip("/")
    pattern = _GO_PATH if kind == GO else _PLAIN_NAME
    if not pattern.fullmatch(name) or not _PLAIN_VERSION.fullmatch(version):
        out.undetermined()
        return
    out.add(_item(kind, name, version or None))


def _index(kind: str, value: str, out: _Collector) -> None:
    location = normalize_location(value) if "://" in value else None
    if location is None:
        out.undetermined()
        return
    out.add(_item(INDEX, f"{kind}:{location}"))


def _operands(
    tokens: list[str], value_options: frozenset[str], special: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """オプションを読み飛ばし、(種類, 語) の並びにする。種類は空か、特別扱いするオプションの名前。

    値を取ると分かっているオプションは値ごと読み飛ばす。特別扱いするオプションは値と組で返す。
    """
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            out.extend(("", t) for t in tokens[i + 1:])
            break
        if tok.startswith("-") and len(tok) > 1:
            name, eq, value = tok.partition("=")
            # 短いオプションは値を続けて付けられる。-rhttps://... は -r とその値である。
            # 未知のオプションとして捨てると、値を付けるだけで取得先が判定から外れる。
            attached = not tok.startswith("--") and len(tok) >= 3
            if name in special or name == "--hash":
                if eq:
                    out.append((name, value))
                elif i + 1 < len(tokens):
                    out.append((name, tokens[i + 1]))
                    i += 1
            elif not eq and name in value_options:
                i += 1
            elif attached:
                # 束ねた短いオプションは 1 字ずつ読む。値を取る最初の字より後ろがその値である。
                # 先頭の 2 字だけを見ると、-qrURL のようにフラグを前に束ねるだけで値が消える。
                for k in range(1, len(tok)):
                    letter = "-" + tok[k]
                    if letter in special or letter in value_options:
                        attached_value = tok[k + 1 :]
                        if not attached_value and i + 1 < len(tokens):
                            attached_value = tokens[i + 1]
                            i += 1
                        if letter in special and attached_value:
                            out.append((letter, attached_value))
                        break
            i += 1
            continue
        out.append(("", tok))
        i += 1
    return out


def _pip_install(args: list[str], out: _Collector) -> None:
    special = _PIP_INDEX_OPTIONS | _PIP_MANIFEST_OPTIONS | _PIP_EDITABLE_OPTIONS
    operands = _operands(args, _PIP_VALUE_OPTIONS, special)
    digest = None
    for kind, tok in operands:
        if kind == "--hash" and _HASH_VALUE.fullmatch(tok.lower()):
            digest = tok.lower()
    for kind, tok in operands:
        if kind in _PIP_INDEX_OPTIONS:
            _index(PYPI, tok, out)
        elif kind in _PIP_MANIFEST_OPTIONS:
            if _is_location(tok):
                _add_location(tok, out)
            else:
                out.add(_item(MANIFEST, f"{PYPI}:{tok.strip().replace(chr(92), '/')}"))
        elif kind in _PIP_EDITABLE_OPTIONS or kind == "":
            _pypi_spec(tok, out, digest)


def _runner(args: list[str], out: _Collector, *, install_all: bool = False) -> None:
    """パッケージを取得して実行する道具。最初の語がパッケージで、残りはそのパッケージへの引数。"""
    special = _PIP_INDEX_OPTIONS | _UVX_FROM_OPTIONS | _UVX_WITH_OPTIONS
    operands = _operands(args, _PIP_VALUE_OPTIONS, special)
    explicit = False
    for kind, tok in operands:
        if kind in _PIP_INDEX_OPTIONS:
            _index(PYPI, tok, out)
        elif kind in _UVX_FROM_OPTIONS:
            explicit = True
            _pypi_spec(tok, out)
        elif kind in _UVX_WITH_OPTIONS:
            for part in tok.split(","):
                _pypi_spec(part, out)
    plain = [tok for kind, tok in operands if kind == ""]
    if install_all:
        for tok in plain:
            _pypi_spec(tok, out)
    elif plain and not explicit:
        first = plain[0]
        if "@" in first and not _is_location(first):
            # uvx は 名前@版 で版を指定する。
            name, _, version = first.partition("@")
            first = f"{name}=={version}" if version else name
        _pypi_spec(first, out)


def _npm(args: list[str], out: _Collector, *, exec_first: bool) -> None:
    operands = _operands(args, _NPM_VALUE_OPTIONS, _NPM_INDEX_OPTIONS | _NPM_PACKAGE_OPTIONS)
    named = False
    for kind, tok in operands:
        if kind in _NPM_INDEX_OPTIONS:
            _index(NPM, tok, out)
        elif kind in _NPM_PACKAGE_OPTIONS:
            named = True
            _npm_spec(tok, out)
    plain = [tok for kind, tok in operands if kind == ""]
    if exec_first:
        if plain and not named:
            _npm_spec(plain[0], out)
        return
    if not plain:
        out.add(_item(MANIFEST, f"{NPM}:package.json"))
    for tok in plain:
        _npm_spec(tok, out)


def _npm_create(args: list[str], out: _Collector) -> None:
    """npm create と npm init の初期化子。名前は create- を前に付けたパッケージへ写る。"""
    plain = [tok for tok in args if not tok.startswith("-")]
    if not plain:
        return
    name = plain[0].lower()
    version = ""
    at = name.rfind("@")
    if at > 0:
        name, version = name[:at], name[at:]
    if name.startswith("@"):
        scope, _, rest = name.partition("/")
        package = f"{scope}/create-{rest}" if rest else f"{scope}/create"
    else:
        package = f"create-{name}"
    _npm_spec(package + version, out)


def _program(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def _tool_of(prog: str) -> str | None:
    """導入の道具の名前へ揃える。道具でなければ None。"""
    if _PIP.fullmatch(prog):
        return "pip"
    return prog if prog in _INSTALL_VERBS else None


def _mentions_install(tokens: list[str]) -> bool:
    """語の並びのどこかに、導入の道具とその動詞の組が現れるか。"""
    for j, tok in enumerate(tokens):
        tool = _tool_of(_program(tok))
        if tool is None:
            continue
        verbs = _INSTALL_VERBS[tool]
        if not verbs or any(t.lower() in verbs for t in tokens[j + 1 : j + 6]):
            return True
    return False


# シェルの長いオプションのうち値を取るもの。値を語として読まない。
_SHELL_LONG_VALUE_OPTIONS: Final[frozenset[str]] = frozenset({"--rcfile", "--init-file"})
# シェルの短いオプションのうち値を取るもの。束の中に現れた数だけ後ろの語を値として読み飛ばす。
_SHELL_SHORT_VALUE_OPTIONS: Final[str] = "oO"


def _shell_command(rest: list[str]) -> tuple[bool, str | None]:
    """シェルの引数から -c のコマンド文字列を取り出す。

    返り値は (-c があるか, コマンド文字列)。-c があるのにコマンド文字列を読めなければ None を返し、
    呼び出し側は判別できない取得として扱う。-c は短いオプションの束の中だけで探す。--norc の
    ような長いオプションの中の字を -c と取り違えると、本物のコマンドを読まずに終える。コマンド
    文字列は -c の後の最初のオプションでない語である。-c より先にオプションでない語が来たら、
    それはスクリプトなので -c は無い。
    """
    seen_c = False
    j = 0
    while j < len(rest):
        tok = rest[j]
        if tok in ("--", "-"):
            if not seen_c:
                return False, None
            return True, rest[j + 1] if j + 1 < len(rest) else None
        if tok.startswith("--"):
            name, eq, _value = tok.partition("=")
            j += 2 if (name in _SHELL_LONG_VALUE_OPTIONS and not eq) else 1
            continue
        if tok[:1] in ("-", "+") and len(tok) > 1:
            letters = tok[1:]
            if tok[0] == "-" and "c" in letters:
                seen_c = True
            j += 1 + sum(letters.count(c) for c in _SHELL_SHORT_VALUE_OPTIONS)
            continue
        if seen_c:
            return True, tok
        return False, None
    return seen_c, None


def _command(tokens: list[str], out: _Collector, nesting: int) -> None:
    """1 つのコマンドの語の並びから取得を読む。"""
    i = 0
    while i < len(tokens):
        prog = _program(tokens[i])
        assignment = _ASSIGNMENT.fullmatch(tokens[i])
        if assignment:
            name, _, value = tokens[i].partition("=")
            kind = _INDEX_VARIABLES.get(name.upper())
            if kind is not None and value:
                for part in value.replace(",", " ").split():
                    if part.lower() not in ("off", "direct"):
                        _index(kind, part, out)
            i += 1
            continue
        if prog == "eval":
            # eval は後ろの語をつないでシェルとして実行する。
            if nesting >= MAX_SHELL_NESTING:
                out.undetermined()
                return
            _text(" ".join(tokens[i + 1:]), out, nesting + 1)
            return
        if prog not in _WRAPPERS:
            break
        values = _WRAPPERS[prog]
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            name, eq, value = tokens[i].partition("=")
            if prog == "env" and name in ("-S", "--split-string"):
                # env -S は値を語に分けて実行する。値の中身を読む。
                inner = value if eq else (tokens[i + 1] if i + 1 < len(tokens) else "")
                if nesting >= MAX_SHELL_NESTING:
                    out.undetermined()
                    return
                _text(inner, out, nesting + 1)
                return
            i += 2 if (tokens[i] in values and not eq) else 1
    if i >= len(tokens):
        return
    prog = _program(tokens[i])
    if _tool_of(prog) is None and prog not in _SHELLS and not _PYTHON.fullmatch(prog):
        # 道具の名前ではない語の後ろに導入の手順が続く。前に置く語の働きを 1 つずつは読まない。
        # 取得先は判別できないものとして印を立てる。
        if _mentions_install(tokens[i + 1 :]):
            out.undetermined()
        return
    rest = tokens[i + 1:]
    if prog in _SHELLS:
        found, script = _shell_command(rest)
        if not found:
            return
        if script is None or nesting >= MAX_SHELL_NESTING:
            out.undetermined()
            return
        _text(script, out, nesting + 1)
        return
    if _PYTHON.fullmatch(prog):
        if len(rest) >= 2 and rest[0] == "-m":
            prog, rest = _program(rest[1]), rest[2:]
        elif _mentions_install(rest):
            out.undetermined()
            return
        else:
            return
    # 動詞の前の共通のオプションを読み飛ばす。値を取ると分かっているものは値ごと飛ばす。
    j = 0
    while j < len(rest) and rest[j].startswith("-") and rest[j] != "--":
        name, eq, _value = rest[j].partition("=")
        j += 2 if (name in _GLOBAL_VALUE_OPTIONS and not eq) else 1
    skipped = j > 0
    rest = rest[j:]
    verb = rest[0].lower() if rest else ""
    args = rest[1:]
    tool = _tool_of(prog)
    # オプションの値を動詞と取り違えた可能性がある。後ろに導入の動詞が残っていれば判別できない。
    if (
        skipped
        and tool is not None
        and _INSTALL_VERBS[tool]
        and verb not in _INSTALL_VERBS[tool]
        and any(t.lower() in _INSTALL_VERBS[tool] for t in rest[:6])
    ):
        out.undetermined()
        return
    if _PIP.fullmatch(prog):
        if verb in ("install", "download", "wheel"):
            _pip_install(args, out)
    elif prog == "uv":
        if verb == "pip" and args[:1] == ["install"]:
            _pip_install(args[1:], out)
        elif verb == "add":
            _pip_install(args, out)
        elif verb == "tool" and args[:1] in (["install"], ["run"]):
            _runner(args[1:], out)
        elif verb == "run":
            # uv run は手元の計画を実行する。--with と --from で足した依存だけが取得である。
            special = _UVX_FROM_OPTIONS | _UVX_WITH_OPTIONS | _PIP_INDEX_OPTIONS
            for kind, tok in _operands(args, _PIP_VALUE_OPTIONS, special):
                if kind in _PIP_INDEX_OPTIONS:
                    _index(PYPI, tok, out)
                elif kind in _UVX_FROM_OPTIONS:
                    _pypi_spec(tok, out)
                elif kind in _UVX_WITH_OPTIONS:
                    for part in tok.split(","):
                        _pypi_spec(part, out)
    elif prog == "uvx":
        _runner(rest, out)
    elif prog == "pipx":
        if verb == "run":
            _runner(args, out)
        elif verb == "install":
            _runner(args, out, install_all=True)
        elif verb == "inject":
            _runner(args[1:], out, install_all=True)
    elif prog in ("poetry", "pdm", "rye", "hatch") and verb == "add":
        _pip_install(args, out)
    elif prog in ("npm", "pnpm", "yarn", "bun", "cnpm"):
        if verb in _NPM_INSTALL_VERBS:
            _npm(args, out, exec_first=False)
        elif verb == "ci":
            out.add(_item(MANIFEST, f"{NPM}:package-lock.json"))
        elif verb in _NPM_EXEC_VERBS:
            _npm(args, out, exec_first=True)
        elif verb in ("create", "init"):
            _npm_create(args, out)
    elif prog in ("npx", "bunx", "pnpx"):
        _npm(rest, out, exec_first=True)
    elif prog == "cargo" and verb in ("install", "add"):
        for kind, tok in _operands(args, _CARGO_VALUE_OPTIONS, _CARGO_SOURCE_OPTIONS):
            if kind == "--git":
                # --git は常にリポジトリの取得である。https の綴りでも git の取得先として扱う。
                _add_location(tok if tok.lower().startswith("git+") else f"git+{tok}", out)
            elif kind in ("--index", "--registry"):
                if "://" in tok:
                    _index(CRATES, tok, out)
                elif _PLAIN_NAME.fullmatch(tok.lower()):
                    out.add(_item(INDEX, f"{CRATES}:{tok.lower()}"))
                else:
                    out.undetermined()
            elif kind == "":
                _plain_spec(CRATES, tok, out)
    elif prog == "go" and verb in ("get", "install"):
        for kind, tok in _operands(args, _GO_VALUE_OPTIONS):
            if kind == "":
                _plain_spec(GO, tok, out)
    elif prog == "gem" and verb == "install":
        for kind, tok in _operands(args, _GEM_VALUE_OPTIONS, _GEM_INDEX_OPTIONS):
            if kind in _GEM_INDEX_OPTIONS:
                _index(GEM, tok, out)
            elif kind == "":
                _plain_spec(GEM, tok, out, sep=":")
    elif prog == "git" and verb == "clone":
        plain = [tok for kind, tok in _operands(args, _GIT_VALUE_OPTIONS) if kind == ""]
        if plain and not _is_local(plain[0]):
            location = normalize_location(plain[0])
            if location is None:
                out.undetermined()
            else:
                out.add(_item(GIT, location))
    elif prog in ("huggingface-cli", "hf") and verb == "download":
        plain = [tok for kind, tok in _operands(args, _HF_VALUE_OPTIONS) if kind == ""]
        if plain:
            repo = plain[0].lower()
            if _HF_REPO.fullmatch(repo):
                out.add(_item(HF, repo))
            else:
                out.undetermined()


def _split(text: str) -> tuple[list[list[str]], bool]:
    """文字列をシェルの区切りと改行でコマンドの語の並びに分ける。解釈できなかったかも返す。"""
    commands: list[list[str]] = []
    failed = False
    for line in text.splitlines():
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=_PUNCTUATION)
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            # 引用符の対が崩れている。空白で分けて読み、判別が確かでないことを印で示す。
            tokens = line.split()
            failed = True
        # 語の先頭の # から行末はシェルと同じく注記として落とす。語の途中の # は落とさない。
        for k, tok in enumerate(tokens):
            if tok.startswith("#"):
                tokens = tokens[:k]
                break
        current: list[str] = []
        for tok in tokens:
            if tok and set(tok) <= set(_PUNCTUATION):
                if current:
                    commands.append(current)
                current = []
                continue
            current.append(tok)
        if current:
            commands.append(current)
    return commands, failed


def _text(text: str, out: _Collector, nesting: int) -> None:
    if len(text) > MAX_COMMAND_CHARS:
        out.truncated = True
        text = text[:MAX_COMMAND_CHARS]
    commands, failed = _split(text)
    before = len(out.items)
    for tokens in commands:
        _command(tokens, out, nesting)
    if failed and len(out.items) != before:
        out.truncated = True


def _collect(arguments: Any) -> tuple[list[str], list[list[str]], bool]:
    """引数の中の文字列と、文字列だけから成る並びを集める。上限で打ち切ったかも返す。

    再帰を使わない。並びはコマンドを語に分けて渡す形として読む。辞書の鍵は読まない。
    """
    texts: list[str] = []
    argvs: list[list[str]] = []
    cut = False
    budget = MAX_SCANNED_STRINGS
    stack: list[tuple[Any, int]] = [(arguments, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            if budget <= 0:
                cut = True
                break
            budget -= 1
            texts.append(item)
            continue
        if not isinstance(item, (dict, list, tuple)):
            continue
        if depth >= MAX_SCAN_DEPTH:
            cut = True
            continue
        if isinstance(item, (list, tuple)) and item and all(isinstance(x, str) for x in item):
            argvs.append([str(x) for x in item[:MAX_SCANNED_STRINGS]])
        if isinstance(item, dict):
            # 実行の道具の名前と引数の並びを別の鍵で受ける形。名前と並びをつないで 1 つのコマンドとして読む。
            heads = [v for v in item.values() if isinstance(v, str) and v.strip()]
            tails = [
                [str(x) for x in v[:MAX_SCANNED_STRINGS]]
                for v in item.values()
                if isinstance(v, (list, tuple)) and v and all(isinstance(x, str) for x in v)
            ]
            for head in heads[:8]:
                if len(head) > MAX_NAME_LEN or " " in head.strip():
                    continue
                for tail in tails[:8]:
                    argvs.append([head.strip(), *tail])
        children = list(item.values()) if isinstance(item, dict) else list(item)
        stack.extend((child, depth + 1) for child in reversed(children))
    return texts, argvs, cut


def acquisitions_with_overflow(arguments: Any) -> tuple[list[dict[str, str]], bool]:
    """引数に現れる取得と、上限や解釈の失敗で判別できない取得が残ったかを返す。"""
    texts, argvs, cut = _collect(arguments)
    out = _Collector()
    out.truncated = cut
    for argv in argvs:
        _command(argv, out, 0)
    for text in texts:
        _text(text, out, 0)
    return out.items, out.truncated


def acquisition_sources_with_overflow(arguments: Any) -> tuple[list[str], bool]:
    """取得先の並びと、判別できない取得が残ったかを返す。"""
    items, truncated = acquisitions_with_overflow(arguments)
    return [item["source"] for item in items], truncated
