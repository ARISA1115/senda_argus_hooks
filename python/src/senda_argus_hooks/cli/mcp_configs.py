"""端末の MCP クライアントの設定から、MCP サーバの定義を読む。

設定の置き場所は、導入のコマンドが持つ製品ごとの表 INSTALLS の mcp_sources だけから引き、別の表を
持たない。表に置き場所の無い製品と、公式の文書に書かれていない置き場所は読まず、理由を付けて出す。

読み取りだけを行い、設定を書き換えない。走査の対象の一覧と、設定に在るサーバの一覧は、ここの
read_configured_servers だけから作る。コマンドを実行したディレクトリの設定は、そのディレクトリだけを
読み、親をたどらない。
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from senda_argus_hooks.cli.agent_hooks_install import (
    INSTALLS,
    SCOPES,
    InstallError,
    McpSource,
    settings_path,
)

_NO_SOURCE = "MCP サーバの定義の置き場所が公式の文書に書かれていない"

# 1 つの設定のファイルから読むサーバの数の上限。超えた分は読めなかったサーバとして理由を付ける。
MAX_SERVERS_PER_FILE = 256
# 読む設定のファイルの大きさの上限。
MAX_CONFIG_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ConfiguredServer:
    """設定に在る MCP サーバの定義 1 件。definition は設定の値そのままで、秘密を含みうる。"""

    product: str
    scope: str
    path: str
    name: str
    definition: dict[str, Any] = field(hash=False, compare=False)


@dataclass
class ConfigRead:
    servers: list[ConfiguredServer] = field(default_factory=list)
    # 製品の名前から、読み取りに失敗した理由。読めた置き場所がある製品も、失敗した置き場所があれば載せる。
    # 終了コードを 1 にする。
    unreadable_products: dict[str, str] = field(default_factory=dict)
    # 製品の名前から、読まないと決めた置き場所の理由。公式の文書に書かれていない置き場所など。
    refused_products: dict[str, str] = field(default_factory=dict)
    # 読めた設定の中で、サーバとして扱えない定義。製品と場所と名前と理由。
    skipped_servers: list[dict[str, str]] = field(default_factory=list)


def _parse(raw: bytes, fmt: str, path: Path) -> tuple[Any, str | None]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, f"{path} が UTF-8 として読めない"
    if fmt == "toml":
        try:
            import tomllib
        except ImportError:
            return None, f"{path} は TOML で、この Python には tomllib が無い"
        try:
            return tomllib.loads(text), None
        except tomllib.TOMLDecodeError:
            return None, f"{path} が TOML として読めない"
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        # コメント付きの JSON も、黙って空にせず読めない理由として出す。
        return None, f"{path} が JSON として読めない (コメント付きの JSON は読まない)"


def _read_file(path: Path, fmt: str) -> tuple[Any, str | None]:
    """設定のファイルを読む。無いときは (None, None)、読めないときは (None, 理由)。"""
    # FIFO や端末を開くと書き手を待って戻らないため、待たずに開き、通常のファイルだけを読む。
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as exc:
        return None, f"{path} を読めない: {exc.__class__.__name__}"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, f"{path} が通常のファイルでない"
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_CONFIG_BYTES:
            chunk = os.read(fd, min(1024 * 1024, MAX_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        return None, f"{path} を読めない: {exc.__class__.__name__}"
    finally:
        os.close(fd)
    if len(raw) > MAX_CONFIG_BYTES:
        return None, f"{path} が {MAX_CONFIG_BYTES} バイトを超える"
    return _parse(raw, fmt, path)


def _dig(data: Any, key: tuple[str, ...]) -> tuple[Any, str | None]:
    """鍵の並びをたどる。鍵が無いときは (None, None)、途中がオブジェクトでないときは (None, 理由)。"""
    for depth, part in enumerate(key):
        if not isinstance(data, dict):
            where = ".".join(key[:depth]) or "最上位"
            return None, f"{where} がオブジェクトでない"
        data = data.get(part)
        if data is None:
            return None, None
    return data, None


def _source_files(
    product: str,
    source: McpSource,
    *,
    platform: str,
    home: Path,
    system_root: Path,
    cwd: Path,
) -> list[tuple[str, Path]]:
    """置き場所の (方式, ファイル) の並び。"""
    if source.paths is None:
        out: list[tuple[str, Path]] = []
        for scope in SCOPES:
            try:
                out.append(
                    (
                        scope,
                        settings_path(
                            product, scope, platform, home=home, system_root=system_root
                        ),
                    )
                )
            except InstallError:
                continue
        return out
    rel = source.paths.get(platform)
    if rel is None:
        return []
    base = cwd if source.base == "cwd" else home
    return [(source.scope, base / rel)]


def _add_block(
    result: ConfigRead,
    product: str,
    scope: str,
    path: Path,
    block: dict[str, Any],
    counter: list[int],
) -> None:
    for name, definition in block.items():
        where = {"product": product, "scope": scope, "path": str(path)}
        if counter[0] >= MAX_SERVERS_PER_FILE:
            result.skipped_servers.append(
                {
                    **where,
                    "name": str(name)[:256],
                    "reason": f"1 つのファイルのサーバの数の上限 {MAX_SERVERS_PER_FILE} を超えた",
                }
            )
            continue
        counter[0] += 1
        if not isinstance(definition, dict):
            result.skipped_servers.append(
                {**where, "name": str(name)[:256], "reason": "定義がオブジェクトでない"}
            )
            continue
        result.servers.append(
            ConfiguredServer(product, scope, str(path), str(name), definition)
        )


def read_configured_servers(
    *, platform: str, home: Path, system_root: Path, cwd: Path | None = None
) -> ConfigRead:
    """表の全製品の MCP の置き場所から、サーバの定義を読む。"""
    cwd = cwd if cwd is not None else Path.cwd()
    result = ConfigRead()
    seen: set[tuple[str, str, tuple[str, ...], bool]] = set()
    for product, spec in INSTALLS.items():
        if not any(
            source.paths is None or platform in source.paths
            for source in spec.mcp_sources
        ):
            # この OS に対応する置き場所が無い。読み取りの失敗ではないため、断る理由の欄に出す。
            result.refused_products[product] = (
                spec.mcp_platform_refusal or spec.mcp_refusal or _NO_SOURCE
            )
            continue
        if spec.mcp_refusal:
            result.refused_products[product] = spec.mcp_refusal
        reasons: list[str] = []
        previous_exists = False
        for source in spec.mcp_sources:
            files = _source_files(
                product,
                source,
                platform=platform,
                home=home,
                system_root=system_root,
                cwd=cwd,
            )
            exists_here = any(path.exists() for _, path in files)
            if source.only_if_previous_absent and previous_exists:
                # 製品は新の場所のファイルがあれば旧の場所を読まない。両方あるとき旧の定義を送ると、
                # 製品が読み込まない古いサーバを走査した結果として送ってしまう。
                continue
            previous_exists = exists_here
            for scope, path in files:
                # 実行したディレクトリが家のディレクトリと同じとき、同じファイルを二重に読まない。
                try:
                    resolved = str(path.resolve())
                except (OSError, RuntimeError):
                    reasons.append(f"{path} の場所を解決できない")
                    continue
                marker = (product, resolved, source.key, source.per_project)
                if marker in seen:
                    continue
                seen.add(marker)
                data, reason = _read_file(path, source.fmt)
                if reason is not None:
                    reasons.append(reason)
                    continue
                if data is None:
                    continue
                block, shape_error = _dig(data, source.key)
                if shape_error is not None:
                    reasons.append(f"{path} の {shape_error}")
                    continue
                if block is None:
                    continue
                key_name = ".".join(source.key)
                if not isinstance(block, dict):
                    reasons.append(f"{path} の {key_name} がオブジェクトでない")
                    continue
                counter = [0]
                if not source.per_project:
                    _add_block(result, product, scope, path, block, counter)
                    continue
                # プロジェクトごとの表。同じ名前でもプロジェクトが違えば別のサーバとして載せる。
                for project, entry in block.items():
                    if not isinstance(entry, dict):
                        reasons.append(
                            f"{path} の {key_name} の 1 つのプロジェクトの値がオブジェクトでない"
                        )
                        continue
                    servers = entry.get("mcpServers")
                    if servers is None:
                        continue
                    if not isinstance(servers, dict):
                        reasons.append(
                            f"{path} の {key_name}.{project}.mcpServers がオブジェクトでない"
                        )
                        continue
                    _add_block(
                        result, product, f"project:{project}", path, servers, counter
                    )
        if reasons:
            result.unreadable_products[product] = "; ".join(reasons)
    return result
