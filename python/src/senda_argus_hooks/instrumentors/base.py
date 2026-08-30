from __future__ import annotations


class BaseInstrumentor:
    name = "base"

    def instrument(self) -> bool:
        raise NotImplementedError

    def uninstrument(self) -> bool:
        raise NotImplementedError
