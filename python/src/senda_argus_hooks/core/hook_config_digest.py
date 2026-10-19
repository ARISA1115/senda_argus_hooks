"""hook の設定と実行ファイルの要約。canary に載せ、サーバーが管理者の承認した要約と比べる。

要約の対象は 2 つ。

- この部品の Python のファイル。senda_argus_hooks の下の .py を相対のパスの順に読む
- hook の設定のファイル。``SENDA_ARGUS_HOOK_CONFIG_FILES`` に ``os.pathsep`` 区切りで並べる。
  コーディングエージェントの hook の設定 (settings.json など) を置く

各ファイルの名前と内容の SHA-256 を並べた全体の SHA-256 を 16 進 64 文字で返す。

**毎回、内容を全部読んで要約する。** 変更の時刻と大きさは書き換えられるため、内容の証明に使わない。
時刻と大きさで前の値を使い回すと、内容を変えた後に時刻と大きさを戻すだけで古い要約を返し続ける。
ファイルの数と大きさに上限があるため、毎回読む量も上限の中に収まる。

**上限で打ち切ったことは要約に入れる。** ファイルの数か大きさが上限を超えたら、打ち切りのマークを
並びに入れる。マークの入った要約は承認した要約と一致しないため、詰め物で本物のファイルを上限の外へ押し出して
検知を避けることはできない。読めないファイルと、解決できないパスも読めないマークを入れる。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Final, Optional

ENV_CONFIG_FILES: Final[str] = "SENDA_ARGUS_HOOK_CONFIG_FILES"
MAX_FILES: Final[int] = 2000
MAX_FILE_BYTES: Final[int] = 4 * 1024 * 1024
TRUNCATED_MARK: Final[str] = "!truncated"
UNREADABLE_MARK: Final[str] = "!unreadable"
_READ_CHUNK: Final[int] = 64 * 1024


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _expand(item: str) -> Optional[Path]:
    """設定のファイルのパスを解決する。解決できないパスは None を返し、読めないものとして扱う。

    存在しない利用者の ~name は RuntimeError を上げる。上げると要約が作れず canary が止まる。
    """
    try:
        return Path(item).expanduser()
    except (RuntimeError, OSError, ValueError):
        return None


def _targets() -> tuple[list[tuple[str, Optional[Path]]], bool]:
    root = _package_root()
    files = sorted(root.rglob("*.py"))
    out: list[tuple[str, Optional[Path]]] = [
        ("pkg:" + str(p.relative_to(root)).replace(os.sep, "/"), p) for p in files
    ]
    raw = os.environ.get(ENV_CONFIG_FILES, "")
    for item in (x.strip() for x in raw.split(os.pathsep)):
        if item:
            out.append(("cfg:" + item, _expand(item)))
    truncated = len(out) > MAX_FILES
    return out[:MAX_FILES], truncated


def _file_digest(path: Optional[Path]) -> str:
    """内容の SHA-256。上限を超える大きさは打ち切りのマーク、読めなければ読めないマーク。

    大きさは読んだバイト数で数える。stat の大きさを信じて読む量を決めない。
    """
    if path is None:
        return UNREADABLE_MARK
    try:
        h = hashlib.sha256()
        total = 0
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    return TRUNCATED_MARK
                h.update(chunk)
        return h.hexdigest()
    except (OSError, ValueError):
        return UNREADABLE_MARK


def compute() -> str:
    """要約を返す。例外を上げない。"""
    try:
        targets, truncated = _targets()
        lines = [f"{name}\t{_file_digest(path)}" for name, path in targets]
        if truncated:
            lines.append(TRUNCATED_MARK)
    except Exception:  # noqa: BLE001 - 要約を作れないこと自体を、承認と一致しない値として返す
        lines = [UNREADABLE_MARK]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def reset_for_tests() -> None:
    """前の版のキャッシュを消す関数。今はキャッシュを持たないため何もしない。試験の呼び出しのために残す。"""
    return None
