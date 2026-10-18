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


class UsageStream:
    """stream=True の応答を包み、message_start と message_delta の量を集めて、読み終えたときに 1 件だけ記録する。"""

    def __init__(self, inner: Any, on_done: Callable[[dict[str, int]], None]):
        self._inner = inner
        self._on_done = on_done
        self._usage: dict[str, int] = {}
        self._done = False

    def _observe(self, event: Any) -> None:
        kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
        if kind == "message_start":
            message = event.get("message") if isinstance(event, dict) else getattr(event, "message", None)
            self._usage.update(usage_of(message))
        elif kind == "message_delta":
            self._usage.update(usage_of(event))

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        if self._usage:
            with contextlib.suppress(Exception):
                self._on_done(self._usage)

    def __iter__(self):
        try:
            for event in self._inner:
                self._observe(event)
                yield event
        finally:
            self._finish()

    async def __aiter__(self):
        try:
            async for event in self._inner:
                self._observe(event)
                yield event
        finally:
            self._finish()

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

    抜けるときに get_final_message() で残りを読まない。呼び出し側が途中で止めたときに、読み終えるまで
    待たせないため。読んだところまでの current_message_snapshot の量を使う。
    """

    def __init__(self, manager: Any, on_message: Callable[[Any], None], on_error: Callable[[BaseException], None]):
        self._manager = manager
        self._on_message = on_message
        self._on_error = on_error
        self._stream: Any = None

    def _record(self, exc: BaseException | None) -> None:
        with contextlib.suppress(Exception):
            if exc is not None:
                self._on_error(exc)
                return
            snapshot = getattr(self._stream, "current_message_snapshot", None)
            if snapshot is not None and usage_of(snapshot):
                self._on_message(snapshot)

    def __enter__(self):
        self._stream = self._manager.__enter__()
        return self._stream

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._manager.__exit__(exc_type, exc, tb)
        finally:
            self._record(exc)

    async def __aenter__(self):
        self._stream = await self._manager.__aenter__()
        return self._stream

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._manager.__aexit__(exc_type, exc, tb)
        finally:
            self._record(exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)
