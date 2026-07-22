"""モデル識別子の対応判定ヘルパー。

プロバイダ修飾 ID とレスポンスが自己申告するネイティブモデル ID のように、
命名ドメインの異なる識別子を検知側の厳密比較に晒す前に「同一モデルを指すか」を
判定する。表記の正規化はプロバイダごとに規則が異なるため、呼び出し側が
normalizer を渡す。判定は正規化後の完全一致または前方一致とする。
"""

from __future__ import annotations

from typing import Callable


def models_correspond(model_id: str, response_model: str, normalize: Callable[[str], str]) -> bool:
    """リクエストのモデル識別子とレスポンス自己申告モデルが同一モデルを指すか判定する。"""
    if not model_id or not response_model:
        return False
    a = normalize(model_id)
    b = normalize(response_model)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)
