"""音声のセッションで文字起こしが事象として届かない構成のための、ローカルの文字起こしの差し替え口。

提供元のセッションが文字起こしを有効にしていれば、発話は文字起こしの完了の事象として届き、ここは
使わない。無効にした構成では発話の内容が監査に一度も現れないため、顧客の環境の中で動くモデルで
文字に起こす。

**外部に接続しない。** 重みは実在するローカルの場所からだけ読み、モデルの名前を受け付けない。名前を
受けると、ライブラリは不足した重みを取得しに行く。取得を禁じる設定も併せて渡す。

**重みを固定する。** 期待するダイジェストを渡すと、読み込む前に重みのファイルを照合し、一致しなければ
読み込まない。差し替えられた重みで文字起こしを歪められないようにする。

**生の音声は外へ出さない。** 受け取った音声はこのプロセスの中で文字に起こすだけで、送出に載せない。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

# 文字起こしの差し替え口の形。音声の本体と、その形式を受け、文字を返す。起こせなければ None を返す。
# 形式は "pcm16" "g711_ulaw" "g711_alaw" のいずれかと、標本化の周波数である。
Transcriber = Callable[[bytes, "AudioFormat"], "str | None"]


class AudioFormat(Protocol):
    encoding: str
    rate: int


class LocalModelError(ValueError):
    """ローカルの重みとして受け付けられない指定。"""


# faster-whisper の重みのファイル名。CTranslate2 の変換物はこの名前で置かれる。
_WEIGHTS_FILE = "model.bin"


def _verify_local_dir(model_dir: str | os.PathLike[str]) -> Path:
    path = Path(model_dir)
    # 実在するディレクトリだけを受ける。存在しない値はモデルの名前として解釈され、取得に行く。
    if not path.is_dir():
        raise LocalModelError(f"ローカルの重みのディレクトリが存在しない: {path}")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_weights(model_dir: str | os.PathLike[str], weights_sha256: str | None) -> Path:
    """重みのディレクトリを確認し、期待するダイジェストがあれば照合する。"""
    path = _verify_local_dir(model_dir)
    if weights_sha256:
        weights = path / _WEIGHTS_FILE
        if not weights.is_file():
            raise LocalModelError(f"重みのファイルが無い: {weights}")
        actual = _sha256_file(weights)
        expected = weights_sha256.lower().removeprefix("sha256:")
        if actual != expected:
            raise LocalModelError("重みのダイジェストが期待する値と一致しない")
    return path


def _pcm16_to_float(audio: bytes, rate: int, target_rate: int = 16_000) -> Any:
    """16 ビットの PCM を、モデルが受ける周波数の浮動小数の列にする。"""
    import numpy as np  # 任意の依存。faster-whisper が要求する

    samples = np.frombuffer(audio[: len(audio) - (len(audio) % 2)], dtype="<i2").astype("float32") / 32768.0
    if rate == target_rate or samples.size == 0:
        return samples
    count = int(samples.size * target_rate / rate)
    positions = np.linspace(0, samples.size - 1, num=count, dtype="float64")
    return np.interp(positions, np.arange(samples.size), samples).astype("float32")


def faster_whisper_transcriber(
    model_dir: str | os.PathLike[str],
    *,
    weights_sha256: str | None = None,
    device: str = "cpu",
    compute_type: str = "int8",
    language: str | None = None,
) -> Transcriber:
    """faster-whisper のローカルの重みで文字に起こす差し替え口を返す。

    faster-whisper は任意の依存である。入っていなければ ImportError を出す。重みの場所の確認と
    ダイジェストの照合は、ライブラリを読み込む前に行う。
    """
    path = verify_weights(model_dir, weights_sha256)
    # ライブラリの取得の経路も閉じる。重みは場所で渡しているが、付随するファイルの探索で取得に
    # 行く実装がありうる。
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    model = WhisperModel(str(path), device=device, compute_type=compute_type, local_files_only=True)

    def transcribe(audio: bytes, fmt: AudioFormat) -> str | None:
        # 復号できる形式だけを扱う。判別できない形式は起こさない。
        if fmt.encoding != "pcm16":
            return None
        samples = _pcm16_to_float(audio, fmt.rate)
        segments, _info = model.transcribe(samples, language=language)
        text = " ".join(seg.text.strip() for seg in segments if getattr(seg, "text", ""))
        return text or None

    return transcribe
