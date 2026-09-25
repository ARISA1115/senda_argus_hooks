"""LLM レスポンスからメタデータを抽出する共通ヘルパー。

各 instrumentor (openai/anthropic/litellm/ollama) が個別に持っていた
response_model 抽出を共通化する。LangChain の LLMResult は llm_output 配下に
モデル名を載せる別形状のため、integrations/langchain.py 側の専用抽出を使う。
"""

from __future__ import annotations

from typing import Any


def extract_response_model(response: Any) -> str | None:
    """レスポンスが自己申告する実モデル識別子を抽出する。

    リクエストで指定した model との不一致を Argus 側の ModelSwapRule が
    検知できるよう、response_model として別フィールドで送出するために使う。
    属性アクセス (pydantic 応答) と dict の両形状を受理し、取得できない場合は
    None を返す。
    """
    model = getattr(response, "model", None)
    if model is None and isinstance(response, dict):
        model = response.get("model")
    if isinstance(model, str) and model.strip():
        return model
    return None
