"""OpenAI の応答と stream=True の事象から、使用量を同じ形へ読むための部品。

OpenAI の応答は、チャットが prompt_tokens と completion_tokens、Responses が input_tokens と output_tokens で
量を返す (openai 3.35.1 の types/completion_usage.py と types/responses/response_usage.py で確認)。
どちらも **入力はキャッシュされたトークンを含む**。Anthropic の input_tokens はキャッシュを含まず、別の項目に
分かれる。この違いのため、OpenAI の記録では、キャッシュの量を cache_read_input_tokens の名前で載せない。載せると、
入力へキャッシュの量を足す側が、二重に数える。キャッシュの量は cached_input_tokens の名前で、情報として載せる。
"""

from __future__ import annotations

from typing import Any

from .usage_stream import StreamState


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def usage_of_openai(usage: Any) -> dict[str, int]:
    """チャット、Responses のどちらの使用量も、input_tokens と output_tokens へ寄せる。

    入力と出力の一方でも整数で読めないときは、その項目を載せない。載せない項目は、突合で量の無い記録として
    数えられ、その日を判定しない。0 に倒さない。
    """
    out: dict[str, int] = {}
    input_tokens = _int(_get(usage, "prompt_tokens"))
    if input_tokens is None:
        input_tokens = _int(_get(usage, "input_tokens"))
    output_tokens = _int(_get(usage, "completion_tokens"))
    if output_tokens is None:
        output_tokens = _int(_get(usage, "output_tokens"))
    if input_tokens is not None:
        out["input_tokens"] = input_tokens
    if output_tokens is not None:
        out["output_tokens"] = output_tokens
    cached = _int(_get(_get(usage, "prompt_tokens_details"), "cached_tokens"))
    if cached is None:
        cached = _int(_get(_get(usage, "input_tokens_details"), "cached_tokens"))
    if cached is not None:
        out["cached_input_tokens"] = cached
    audio_in = _int(_get(_get(usage, "prompt_tokens_details"), "audio_tokens"))
    if audio_in is not None:
        out["input_audio_tokens"] = audio_in
    audio_out = _int(_get(_get(usage, "completion_tokens_details"), "audio_tokens"))
    if audio_out is not None:
        out["output_audio_tokens"] = audio_out
    return out


_RESPONSE_DONE = ("response.completed", "response.incomplete", "response.failed")
_RESPONSE_START = ("response.created", "response.in_progress")


def observe_openai(event: Any, state: StreamState) -> None:
    """チャットの断片と Responses の事象から、応答のモデルと量を集める。

    チャットの量は、stream_options の include_usage を要求したときだけ、最後の断片に載る。要求しなければ、
    量は返らない。Responses は、完了の事象の response に量が必ず載る。
    """
    kind = _get(event, "type")
    if isinstance(kind, str) and kind.startswith("response."):
        response = _get(event, "response")
        model = _get(response, "model")
        if isinstance(model, str) and model and (kind in _RESPONSE_START or kind in _RESPONSE_DONE):
            state.model = model
        if kind in _RESPONSE_DONE:
            state.usage.update(usage_of_openai(_get(response, "usage")))
        return
    model = _get(event, "model")
    if isinstance(model, str) and model:
        state.model = model
    usage = _get(event, "usage")
    if usage is not None:
        state.usage.update(usage_of_openai(usage))
