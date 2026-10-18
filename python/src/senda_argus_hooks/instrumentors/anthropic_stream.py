"""Anthropic の stream=True と messages.stream() の応答から、使用量を集めるための部品。

事象の読み方 (observe_anthropic) と、messages.stream() の管理オブジェクトの包みを持つ。stream=True の
ストリームの包みは、提供元に依らない usage_stream.UsageStream が持つ。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from .usage_stream import StreamedMessage, StreamState

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def usage_of(obj: Any) -> dict[str, int]:
    usage = obj.get("usage") if isinstance(obj, dict) else getattr(obj, "usage", None)
    out: dict[str, int] = {}
    if usage is None:
        return out
    for name in USAGE_FIELDS:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            out[name] = value
    return out


def observe_anthropic(event: Any, state: StreamState) -> None:
    """message_start の量と応答のモデル、message_delta の量を集める。"""
    kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    if kind == "message_start":
        message = event.get("message") if isinstance(event, dict) else getattr(event, "message", None)
        state.usage.update(usage_of(message))
        model = message.get("model") if isinstance(message, dict) else getattr(message, "model", None)
        if isinstance(model, str) and model:
            state.model = model
    elif kind == "message_delta":
        state.usage.update(usage_of(event))


# 提供元の SDK と通信の部品の型。with の中で上がったこれらは、利用者のコードでなく、ストリームの読み取りの失敗。
_PROVIDER_ERROR_ROOTS = ("anthropic", "openai", "httpx", "httpx2", "httpcore")


def _is_provider_error(exc: BaseException) -> bool:
    return type(exc).__module__.split(".", 1)[0] in _PROVIDER_ERROR_ROOTS


def _snapshot_of(stream: Any) -> Any:
    """読んだところまでの応答。まだ 1 つも読んでいないストリームは None を返す。

    SDK の current_message_snapshot は、事象を読む前に読むと AssertionError を上げる (anthropic 1.15.0 の
    lib/streaming/_messages.py で確認)。同じ型が持つ _current_snapshot_or_none は、読む前に None を返す。
    後者があれば使い、無い版では前者を、失敗を None として読む。
    """
    safe = getattr(stream, "_current_snapshot_or_none", None)
    if callable(safe):
        return safe()
    try:
        return getattr(stream, "current_message_snapshot", None)
    except Exception:  # noqa: BLE001 - 読む前の assert を、量が無いこととして扱う
        return None


class UsageStreamManager:
    """messages.stream() の管理オブジェクトを包み、with を抜けるときに、その時点の量を 1 件だけ記録する。

    - 抜けるときに get_final_message() で残りを読まない。呼び出し側が途中で止めたときに、読み終えるまで待たせない。
      読んだところまでの current_message_snapshot の量を使う。
    - with の中の例外は、量があっても記録する。課金は済んでいるため、量を捨てると Argus 側が少なくなる。量が無い
      まま例外で抜けたときは、型の名前だけを llm.error に載せる。利用者のコードの例外の文を送らない。
    - 要求は __enter__ で出る。ここで失敗したときは、例外の文を含めて llm.error にする。
    """

    def __init__(
        self,
        manager: Any,
        on_message: Callable[[Any], None],
        on_error: Callable[[BaseException, bool], None],
    ):
        self._manager = manager
        self._on_message = on_message
        self._on_error = on_error
        self._stream: Any = None

    def _record_exit(self, exc: BaseException | None) -> None:
        with contextlib.suppress(Exception):
            snapshot = _snapshot_of(self._stream)
            has_usage = snapshot is not None and bool(usage_of(snapshot))
            provider_error = exc is not None and _is_provider_error(exc)
            if has_usage:
                # 途中までの量は、課金された分として残す。
                self._on_message(snapshot)
            if exc is not None and (provider_error or not has_usage):
                # 提供元か通信の失敗は、量が残っていても、失敗として記録する。利用者のコードの例外は、
                # 型の名前だけを載せ、例外の文を送らない。
                self._on_error(exc, provider_error)
            elif exc is None and not has_usage:
                # 何も読まずに抜けた。要求は __enter__ で出ているため、量の無い記録として残す。数えないと、
                # 量の無い呼び出しが、Argus 側が少ないことに気付けない形で消える。
                self._on_message(StreamedMessage(None, {}))

    def _record_enter_failure(self, exc: BaseException) -> None:
        with contextlib.suppress(Exception):
            self._on_error(exc, True)

    def __enter__(self):
        try:
            self._stream = self._manager.__enter__()
        except BaseException as exc:
            self._record_enter_failure(exc)
            raise
        return self._stream

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._manager.__exit__(exc_type, exc, tb)
        finally:
            self._record_exit(exc)

    async def __aenter__(self):
        try:
            self._stream = await self._manager.__aenter__()
        except BaseException as exc:
            self._record_enter_failure(exc)
            raise
        return self._stream

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._manager.__aexit__(exc_type, exc, tb)
        finally:
            self._record_exit(exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)
