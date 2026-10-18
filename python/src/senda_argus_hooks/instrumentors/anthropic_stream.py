"""Anthropic の stream=True と messages.stream() の応答から、使用量を集めて 1 件だけ記録するための包み。

SDK のストリームと同じ使い方 (for、async for、with、close) を保つ。包まれたオブジェクトの他の属性は
そのまま委譲する。記録の失敗は呼び出しへ伝えない。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class StreamedMessage:
    """読み終えたストリームから組み立てた、量だけを持つ応答。共通の記録の処理が、普通の応答と同じに扱う。"""

    def __init__(self, model: Any, usage: dict[str, int]):
        self.model = model
        self.usage = dict(usage)

    def model_dump(self) -> dict[str, Any]:
        return {"model": self.model, "usage": dict(self.usage), "content": []}


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


class UsageStream:
    """stream=True の応答を包み、message_start と message_delta の量を集めて、読み終えたときに 1 件だけ記録する。

    for、async for、next、anext、with、close を、SDK のストリームと同じに使える。応答のモデルは message_start の
    message.model から取る。要求に渡した別名でなく、応答のモデルの名前で記録するため。
    """

    def __init__(self, inner: Any, on_done: Callable[[dict[str, int], str | None], None]):
        self._inner = inner
        self._on_done = on_done
        self._usage: dict[str, int] = {}
        self._model: str | None = None
        self._done = False
        self._it: Any = None
        self._ait: Any = None

    def _observe(self, event: Any) -> None:
        kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
        if kind == "message_start":
            message = event.get("message") if isinstance(event, dict) else getattr(event, "message", None)
            self._usage.update(usage_of(message))
            model = message.get("model") if isinstance(message, dict) else getattr(message, "model", None)
            if isinstance(model, str) and model:
                self._model = model
        elif kind == "message_delta":
            self._usage.update(usage_of(event))

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        if self._usage:
            with contextlib.suppress(Exception):
                self._on_done(self._usage, self._model)

    def __iter__(self):
        return self

    def __next__(self):
        if self._it is None:
            self._it = iter(self._inner)
        try:
            event = next(self._it)
        except StopIteration:
            self._finish()
            raise
        except BaseException:
            self._finish()
            raise
        self._observe(event)
        return event

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._ait is None:
            self._ait = self._inner.__aiter__()
        try:
            event = await self._ait.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException:
            self._finish()
            raise
        self._observe(event)
        return event

    def __enter__(self):
        enter = getattr(self._inner, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc_info):
        try:
            exit_ = getattr(self._inner, "__exit__", None)
            return exit_(*exc_info) if exit_ is not None else None
        finally:
            self._finish()

    async def __aenter__(self):
        enter = getattr(self._inner, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, *exc_info):
        try:
            exit_ = getattr(self._inner, "__aexit__", None)
            return await exit_(*exc_info) if exit_ is not None else None
        finally:
            self._finish()

    def close(self):
        try:
            return self._inner.close()
        finally:
            self._finish()

    async def aclose(self):
        try:
            return await self._inner.close()
        finally:
            self._finish()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


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
            if snapshot is not None and usage_of(snapshot):
                self._on_message(snapshot)
            elif exc is not None:
                self._on_error(exc, False)

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
