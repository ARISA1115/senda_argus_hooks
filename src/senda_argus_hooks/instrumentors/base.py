from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class BaseInstrumentor:
    name = "base"

    def instrument(self) -> bool:
        raise NotImplementedError

    def uninstrument(self) -> bool:
        raise NotImplementedError


@contextmanager
def audit_guard(operation: str) -> Iterator[None]:
    """観測の後処理を本来の呼び出しから隔離する。

    計装対象の呼び出しが成功した後に行うイベント送出やペイロード組み立てが失敗しても、
    呼び出し元へ例外を波及させない。観測は本番の推論経路に対して安全側に倒す。

    呼び出し自体の失敗はこのガードの外で捕捉し、従来どおり error イベントを送出した
    うえで送出する。観測の失敗と本来の呼び出しの失敗を混同しない。

    KeyboardInterrupt や SystemExit は制御フローのため捕まえない。
    """
    try:
        yield
    except Exception:
        logger.warning("senda_argus_hooks の観測処理に失敗しました operation=%s", operation, exc_info=True)
