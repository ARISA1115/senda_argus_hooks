"""hook の設定と実行ファイルの要約。canary に載せ、サーバーが管理者の承認した要約と比べる。

要約の対象は 2 つ。

- この部品の Python のファイル。senda_argus_hooks の下の .py を相対のパスの順に読む
- hook の設定のファイル。``SENDA_ARGUS_HOOK_CONFIG_FILES`` に ``os.pathsep`` 区切りで並べる。
  コーディングエージェントの hook の設定 (settings.json など) を置く

各ファイルの名前と内容の SHA-256 を並べた全体の SHA-256 を 16 進 64 文字で返す。

**上限で打ち切ったことは要約に入れる。** ファイルの数か大きさが上限を超えたら、打ち切りのマークを
並びに入れる。マークの入った要約は承認した要約と一致しないため、詰め物で本物のファイルを上限の外へ押し出して
検知を避けることはできない。読めないファイルも読めないマークを入れる。

同じ内容の再計算を避けるため、ファイルの変更の時刻と大きさが変わらない間は前の値を使う。
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Final, Optional

ENV_CONFIG_FILES: Final[str] = "SENDA_ARGUS_HOOK_CONFIG_FILES"
MAX_FILES: Final[int] = 2000
MAX_FILE_BYTES: Final[int] = 4 * 1024 * 1024
TRUNCATED_MARK: Final[str] = "!truncated"
UNREADABLE_MARK: Final[str] = "!unreadable"

_lock = threading.Lock()
_cache: Optional[tuple[tuple[tuple[str, int, int], ...], str]] = None


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _targets() -> tuple[list[tuple[str, Path]], bool]:
    root = _package_root()
    files = sorted(root.rglob("*.py"))
    out = [("pkg:" + str(p.relative_to(root)).replace(os.sep, "/"), p) for p in files]
    raw = os.environ.get(ENV_CONFIG_FILES, "")
    for item in (x.strip() for x in raw.split(os.pathsep)):
        if item:
            out.append(("cfg:" + item, Path(item).expanduser()))
    truncated = len(out) > MAX_FILES
    return out[:MAX_FILES], truncated


def _stamp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return -1, -1


def compute() -> str:
    """要約を返す。例外を上げない。"""
    global _cache
    targets, truncated = _targets()
    stamps = tuple((name, *_stamp(p)) for name, p in targets) + (
        (TRUNCATED_MARK, int(truncated), 0),
    )
    with _lock:
        if _cache is not None and _cache[0] == stamps:
            return _cache[1]
    lines: list[str] = []
    for (name, path), (_, _, size) in zip(targets, stamps):
        if size < 0:
            lines.append(f"{name}\t{UNREADABLE_MARK}")
            continue
        if size > MAX_FILE_BYTES:
            lines.append(f"{name}\t{TRUNCATED_MARK}")
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = UNREADABLE_MARK
        lines.append(f"{name}\t{digest}")
    if truncated:
        lines.append(TRUNCATED_MARK)
    value = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    with _lock:
        _cache = (stamps, value)
    return value


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
