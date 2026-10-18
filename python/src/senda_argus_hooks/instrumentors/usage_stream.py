"""stream=True の応答から、使用量を集めて 1 件だけ記録するための、提供元に依らない包み。

SDK のストリームと同じ使い方 (for、async for、next、anext、with、close) を保つ。包まれたオブジェクトの
他の属性はそのまま委譲する。記録の失敗は呼び出しへ伝えない。事象の読み方は、提供元ごとの observe が持つ。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any


class StreamedMessage:
    """読み終えたストリームから組み立てた、量だけを持つ応答。共通の記録の処理が、普通の応答と同じに扱う。"""

    def __init__(self, model: Any, usage: dict[str, int]):
        self.model = model
        self.usage = dict(usage)

    def model_dump(self) -> dict[str, Any]:
        return {"model": self.model, "usage": dict(self.usage), "content": []}


class StreamState:
    """読んだ事象から集めた量と、応答のモデル。observe が更新する。"""

    __slots__ = ("usage", "model")

    def __init__(self) -> None:
        self.usage: dict[str, Any] = {}
        self.model: str | None = None


class UsageStream:
    """stream=True の応答を包み、事象から量を集めて、読み終えたときに 1 件だけ記録する。

    for、async for、next、anext、with、close を、SDK のストリームと同じに使える。応答のモデルと量の読み方は、
    提供元ごとの observe が決める。要求に渡した別名でなく、応答のモデルの名前で記録するため。
    """

    def __init__(
        self,
        inner: Any,
        on_done: Callable[[dict[str, Any], str | None], None],
        observe: Callable[[Any, StreamState], None],
        on_error: Callable[[BaseException], None] | None = None,
    ):
        self._inner = inner
        self._on_done = on_done
        self._on_error = on_error
        self._observe_event = observe
        self._state = StreamState()
        self._done = False
        self._it: Any = None
        self._ait: Any = None

    def _observe(self, event: Any) -> None:
        with contextlib.suppress(Exception):
            self._observe_event(event, self._state)

    def _finish(self) -> None:
        """読み終えたとき、閉じたとき、例外で抜けたときに 1 回だけ記録する。

        量が空でも記録する。量が無い記録は、突合で捨てた記録として数えられ、その日を判定しない。何も記録
        しないと、量の無い呼び出しが数えられないまま、Argus 側が少ないことに気付けない。
        """
        if self._done:
            return
        self._done = True
        with contextlib.suppress(Exception):
            self._on_done(self._state.usage, self._state.model)

    def _fail(self, exc: BaseException) -> None:
        """読んでいる途中で、提供元か通信の失敗が上がったとき。

        それまでに量を読んでいれば、課金された分として、量を持つ記録を残す。そのうえで、失敗を llm.error として
        記録する。失敗を成功の記録だけにすると、失敗した呼び出しが成功に見える。量が無ければ、成功の記録は残さない。
        読んでいる途中の例外は、ストリームの読み取りが上げたもので、利用者のコードの例外ではない。
        """
        if self._done:
            return
        self._done = True
        with contextlib.suppress(Exception):
            if self._state.usage:
                self._on_done(self._state.usage, self._state.model)
        if self._on_error is not None:
            with contextlib.suppress(Exception):
                self._on_error(exc)

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
        except Exception as exc:
            self._fail(exc)
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
        except Exception as exc:
            self._fail(exc)
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


