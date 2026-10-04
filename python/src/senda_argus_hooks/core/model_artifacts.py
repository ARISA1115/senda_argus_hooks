"""ローカルで読み込んだモデルの成果物の形式とダイジェスト。

形式は成果物の先頭の数バイトから判別する。拡張子や利用者の申告は使わない。名前の付け方で形式を
偽れないようにし、宣言と実際の読み込みが食い違う場合に実際の方を示す。

判別する形式は次のとおり。読み込みの時にコードを実行しうる直列化は pickle と、pickle を中に持つ
zip の書庫と、joblib の書式である。圧縮した成果物は中身を先頭から判別できないため、読み込みに
使った関数で決める。joblib.load が読んだ圧縮の成果物は、joblib が書く圧縮した pickle である。safetensors と GGUF はコードを実行しない。判別できなければ
unknown にし、安全な側へ倒さない。

ダイジェストは成果物全体の SHA-256 で、読み込みのたびに全体を読み直す。**前の値を使い回さない。**
大きさと更新時刻は書き換えた側が元へ戻せるため、それを鍵にした控えは差し替えを見逃す。上限を
超える成果物はダイジェストを付けず、打ち切ったことを印で示す。
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from typing import Any

# ダイジェストを求める成果物の大きさの上限。既定は 64 GiB。環境変数で変えられる。
_DEFAULT_DIGEST_MAX_BYTES = 64 * 1024**3
_DIGEST_MAX_ENV = "SENDA_ARGUS_MODEL_DIGEST_MAX_BYTES"
_CHUNK = 4 * 1024 * 1024


def _digest_max_bytes() -> int:
    raw = os.environ.get(_DIGEST_MAX_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_DIGEST_MAX_BYTES
    return value if value > 0 else _DEFAULT_DIGEST_MAX_BYTES


def sniff_format(path: str) -> str:
    """先頭の数バイトから直列化の形式を判別する。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return "unknown"
    return _sniff_head(head, path)


def _sniff_head(head: bytes, zip_source: Any) -> str:
    """先頭の数バイトから形式を決める。zip の中身は zip_source から読む。"""
    if head.startswith(b"GGUF"):
        return "gguf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(zip_source) as zf:
                names = zf.namelist()
        except (OSError, zipfile.BadZipFile):
            return "unknown"
        if any(name.endswith(".pkl") for name in names):
            return "pytorch_zip"
        return "zip"
    if len(head) >= 2 and head[0] == 0x80 and 2 <= head[1] <= 5:
        return "pickle"
    if head.startswith(b"\x93NUMPY"):
        return "numpy"
    if len(head) >= 9:
        header_len = int.from_bytes(head[:8], "little")
        if 0 < header_len < 100 * 1024 * 1024 and head[8:9] == b"{":
            return "safetensors"
    zlib_stream = len(head) >= 2 and head[0] == 0x78 and ((head[0] << 8) | head[1]) % 31 == 0
    if (
        zlib_stream
        or head[:2] == b"\x1f\x8b"
        or head[:3] == b"BZh"
        or head[:6] == b"\xfd7zXZ\x00"
        or head[:4] == b"\x04\x22\x4d\x18"
    ):
        # 圧縮の書式。中身は先頭からは分からない。読み込みに使った関数で決める。
        return "compressed"
    return "unknown"


def artifact_digest(path: str) -> tuple[str | None, bool]:
    """成果物全体の SHA-256 と、上限で打ち切ったかを返す。読めなければ (None, False)。"""
    try:
        stat = os.stat(path)
    except OSError:
        return None, False
    if stat.st_size > _digest_max_bytes():
        return None, True
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None, False
    return "sha256:" + h.hexdigest(), False


def artifact_path_of(value: Any) -> str | None:
    """読み込みの引数が成果物のパスなら文字列で返す。ファイルの入れ物や bytes は None。"""
    if isinstance(value, (str, os.PathLike)):
        try:
            return os.fspath(value)
        except TypeError:
            return None
    return None


def describe_artifact(path: str) -> dict[str, Any]:
    """成果物の記録に載せる項目。パスと形式とダイジェストと大きさ。"""
    digest, truncated = artifact_digest(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = None
    out: dict[str, Any] = {
        "artifact_path": os.path.abspath(path),
        "format": sniff_format(path),
        "size_bytes": size,
    }
    if digest:
        out["artifact_hash"] = digest
    if truncated:
        out["digest_truncated"] = True
    return out



def read_artifact_bytes(fh: Any) -> bytes | None:
    """開いた成果物を上限まで読む。上限を超えれば None。

    一定の大きさの塊で読み、合計が上限を超えた時点で打ち切る。上限の分を一度に確保すると、
    小さな成果物でも既定の上限の大きさの確保を試みて失敗する。fstat の大きさは目安で、読んで
    いる間の伸長も合計で見る。
    """
    limit = _digest_max_bytes()
    if os.fstat(fh.fileno()).st_size > limit:
        return None
    fh.seek(0)
    buf = bytearray()
    while True:
        chunk = fh.read(min(_CHUNK, limit + 1 - len(buf)))
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            return None


def describe_artifact_bytes(data: bytes, path: str) -> dict[str, Any]:
    """読み込みへ渡すのと同じバイト列から、describe_artifact と同じ項目を求める。"""
    return {
        "artifact_path": os.path.abspath(path),
        "size_bytes": len(data),
        "artifact_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        "format": _sniff_head(data[:16], io.BytesIO(data)),
    }
