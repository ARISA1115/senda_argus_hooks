"""端末にあるエージェントと MCP の設定を走査し、Argus の POST /v1/endpoint-scans へ送るコマンド。

台帳に無く、計装も入っていない主体を見つけるための走査で、サーバを起動せず、接続もしない。

送る項目は次の一覧に限る。

- エージェント: hook の製品の名前 (claude_code、codex など)、CLI かアプリか、Argus の hook の導入の有無
- MCP の設定: 製品の名前、設定の場所の種類 (user、cwd など)、宣言されたサーバの名前、コマンドの名前、
  接続先のホスト名

設定の env と headers と args の値は読まない。コマンドは最初の語の末尾の名前だけ、接続先は URL の
ホスト名だけを取り、資格情報とポートとパスと問い合わせを落とす。設定のファイルのパスは送らない。
設定の置き場所は scan-mcp と同じ read_configured_servers から読み、別の表を持たない。
本文には端末の鍵で署名のヘッダーを付ける。鍵が無い端末の報告は、受け取り側で自己申告として区別される。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from senda_argus_hooks.cli.agent_hooks_install import (
    INSTALLS,
    InstallError,
    _contains_ours,
    settings_path,
)
from senda_argus_hooks.cli.mcp_configs import ConfigRead, read_configured_servers
from senda_argus_hooks.cli.scan_mcp import _URL_KEYS
from senda_argus_hooks.core.device_identity import (
    HEADER_HOST,
    HEADER_USER,
    encode_header_value,
    hostname,
    os_user,
    sign_headers,
)

SCAN_PATH = "/v1/endpoint-scans"
REPORT_TYPE = "endpoint_scan"
SCANNER_VERSION = "1.0.0"

# 受け側の上限。超えた分は送らず、打ち切りのマークを立てる。
MAX_AGENTS = 64
MAX_CONFIGS = 64
MAX_SERVERS_PER_CONFIG = 256
MAX_NAME_LEN = 128
MAX_RESPONSE_BYTES = 64 * 1024
MAX_SETTINGS_BYTES = 4 * 1024 * 1024

# 製品の CLI の実行ファイルの名前。公式の導入で PATH に置かれる名前だけを並べる。
CLI_BINARIES: dict[str, tuple[str, ...]] = {
    "claude_code": ("claude",),
    "codex": ("codex",),
    "cursor": ("cursor-agent",),
    "gemini_cli": ("gemini",),
    "copilot_cli": ("copilot",),
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def command_name(value: Any) -> str:
    """コマンドの名前。最初の語の末尾の名前だけ。KEY=value の語は環境変数の代入として飛ばす。"""
    if not isinstance(value, str):
        return ""
    for word in value.split():
        if "=" in word:
            continue
        return re.split(r"[/\\]", word)[-1][:64]
    return ""


def host_name(value: Any) -> str:
    """接続先のホスト名だけ。資格情報、ポート、パス、問い合わせを落とす。読めない値は空にする。"""
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value):
        return ""
    raw = value.strip()
    if "://" not in raw:
        raw = "//" + raw
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError:
        return ""
    return host.rstrip(".").lower()[:253]


def _settings_paths(
    product: str, *, platform: str, home: Path, system_root: Path
) -> list[Path]:
    out: list[Path] = []
    for scope in ("user", "managed"):
        try:
            out.append(
                settings_path(
                    product, scope, platform, home=home, system_root=system_root
                )
            )
        except InstallError:
            continue
    return out


def _hook_installed(paths: list[Path]) -> bool:
    """設定のどれかに、Argus の hook の項目があるか。読めない設定は無いとする。"""
    for path in paths:
        try:
            if not path.is_file() or path.stat().st_size > MAX_SETTINGS_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _contains_ours(data):
            return True
    return False


def build_report(
    *,
    platform: str,
    home: Path,
    system_root: Path,
    cwd: Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[dict[str, Any], ConfigRead]:
    """送る本文と、設定の読み取りの結果を返す。which は PATH の実行ファイルを探す関数で、試験で付け替える。"""
    which = which or shutil.which
    read = read_configured_servers(
        platform=platform, home=home, system_root=system_root, cwd=cwd
    )
    truncated = bool(read.unreadable_products or read.skipped_servers)
    with_servers = {s.product for s in read.servers}
    agents: list[dict[str, Any]] = []
    for product in INSTALLS:
        paths = _settings_paths(
            product, platform=platform, home=home, system_root=system_root
        )
        on_path = any(which(b) for b in CLI_BINARIES.get(product, ()))
        present = on_path or product in with_servers or any(p.exists() for p in paths)
        if not present:
            continue
        agents.append(
            {
                "name": product,
                "form": "cli" if on_path else "app",
                "hook_installed": _hook_installed(paths),
            }
        )
    if len(agents) > MAX_AGENTS:
        agents = agents[:MAX_AGENTS]
        truncated = True

    configs: dict[tuple[str, str], dict[str, Any]] = {}
    for server in read.servers:
        if len(server.name) > MAX_NAME_LEN or _CONTROL.search(server.name):
            truncated = True
            continue
        key = (server.product, server.scope)
        cfg = configs.get(key)
        if cfg is None:
            if len(configs) >= MAX_CONFIGS:
                truncated = True
                continue
            cfg = configs[key] = {
                "client": server.product,
                "location": server.scope,
                "servers": [],
            }
        if len(cfg["servers"]) >= MAX_SERVERS_PER_CONFIG:
            cfg["truncated"] = True
            continue
        d = server.definition
        # 読むのは command と接続先の URL の項目だけ。env、headers、args には触れない。
        url = next(
            (
                d.get(k)
                for k in _URL_KEYS.get(server.product, ("url",))
                if isinstance(d.get(k), str)
            ),
            None,
        )
        entry = {"name": server.name, "command": command_name(d.get("command"))}
        host = host_name(url)
        if host:
            entry["host"] = host
        cfg["servers"].append(entry)
    body: dict[str, Any] = {
        "report_type": REPORT_TYPE,
        "scanner_version": SCANNER_VERSION,
        "agents": agents,
        "mcp_configs": list(configs.values()),
    }
    if truncated:
        body["truncated"] = True
    return body, read


def identity_headers(data: bytes) -> dict[str, str]:
    """署名のヘッダーと、端末の名乗り。鍵の無い端末でもホスト名と利用者名のヘッダーを付ける。

    受け取り側は、署名の無い報告をホスト名ごとの自己申告として分ける。ホスト名が無いと、鍵の無い端末の
    報告が組織で 1 つの鍵に集まるため、受け取り側はホスト名の無い自己申告を受けない。
    """
    headers = dict(sign_headers(data))
    if HEADER_HOST not in headers:
        host = hostname()
        if host:
            headers[HEADER_HOST] = encode_header_value(host)
    if HEADER_USER not in headers:
        user = os_user()
        if user:
            headers[HEADER_USER] = encode_header_value(user)
    return headers


def send_report(
    endpoint: str, api_key: str, body: dict[str, Any], *, timeout: float
) -> tuple[int, Any]:
    """本文を署名して 1 件送る。応答の状態と本文を返す。通信の失敗は状態 0。鍵は表示に載せない。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    headers.update(identity_headers(data))
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
    """scan-endpoint の入口。--send が無いか --dry-run なら、送る本文を表示するだけにする。"""
    from senda_argus_hooks.cli.agent_hooks_install import current_platform

    send = bool(args.send) and not args.dry_run
    body, read = build_report(
        platform=args.platform or current_platform(),
        home=Path(args.home) if args.home else Path.home(),
        system_root=Path(args.system_root) if args.system_root else Path("/"),
    )
    summary: dict[str, Any] = {
        "sent": send,
        "body": body,
        "unreadable_products": read.unreadable_products,
        "skipped_servers": read.skipped_servers,
    }
    failed = False
    if send:
        endpoint = os.environ.get("SENDA_ARGUS_ENDPOINT", "").strip()
        if not endpoint:
            raise ValueError("SENDA_ARGUS_ENDPOINT is required for --send")
        api_key = os.environ.get("SENDA_ARGUS_API_KEY", "").strip()
        timeout = float(int(os.environ.get("SENDA_ARGUS_TIMEOUT", "10") or 10))
        status, payload = send_report(endpoint, api_key, body, timeout=timeout)
        summary["response_status"] = status
        summary["response"] = payload
        failed = status != 200
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failed or read.unreadable_products else 0
