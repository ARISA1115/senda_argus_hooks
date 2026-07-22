from __future__ import annotations

import hashlib
import json
import random
from typing import Any

_SKETCH_SEED: int = 1145141919
_SKETCH_PROJECTION_CACHE: dict[tuple[int, int], list[list[float]]] = {}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def sha256_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _projection_matrix(dim_in: int, dim_out: int) -> list[list[float]]:
    """(dim_in, dim_out) ごとに決定的な乱数射影行列を生成しキャッシュする。

    固定シードから決定的に導出するため、同じ dim_in/dim_out の組み合わせなら
    プロセスやインストールをまたいでも常に同じ行列になり、異なる呼び出し間の
    埋め込みスケッチが比較可能であり続ける。
    """
    key = (dim_in, dim_out)
    cached = _SKETCH_PROJECTION_CACHE.get(key)
    if cached is not None:
        return cached
    matrix: list[list[float]] = []
    for i in range(dim_out):
        row_seed = f"{_SKETCH_SEED}:{dim_in}:{dim_out}:{i}"
        rng = random.Random(row_seed)
        matrix.append([rng.gauss(0.0, 1.0) for _ in range(dim_in)])
    _SKETCH_PROJECTION_CACHE[key] = matrix
    return matrix


def derive_embedding_sketch(vector: Any, dim_out: int = 16) -> list[float] | None:
    """埋め込みベクトルを、元の値を復元できない低次元のランダム射影スケッチに変換する。

    Johnson-Lindenstrauss 型のランダム射影は、固定された射影行列を使う限り
    ベクトル間の相対距離を近似的に保持する。次元を大きく落とすことで、寄せられた
    スケッチだけから元の意味的な埋め込み内容を復元することは実質的に不可能になる
    一方、DBSCAN によるクラスタリング判定に必要な距離関係は保たれる。生ベクトルを
    送信せずに EmbeddingClusteringAnomalyRule を成立させるための一次情報として使う。
    """
    if not isinstance(vector, list) or not vector:
        return None
    if not all(isinstance(v, (int, float)) for v in vector):
        return None
    dim_in = len(vector)
    if dim_in <= dim_out:
        return [float(v) for v in vector]
    matrix = _projection_matrix(dim_in, dim_out)
    scale = 1.0 / (dim_out ** 0.5)
    return [sum(r * v for r, v in zip(row, vector)) * scale for row in matrix]
