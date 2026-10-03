"""ローカルでモデルの成果物を読み込む関数を包み、model.loaded を出す。

包む関数は次のとおり。いずれも成果物のパスを最初の引数に取る。

- torch.load
- safetensors.torch.load_file と safetensors.numpy.load_file
- joblib.load

記録には成果物のパスと、先頭の数バイトから判別した形式と、成果物全体のダイジェストと、読み込みに
使った関数を載せる。torch.load は weights_only の指定も載せる。パスでなくファイルの入れ物を
渡された読み込みは、形式とダイジェストを求められないため、読み込みに使った関数だけを載せる。

**形式とダイジェストは読み込みの前に取る。** pickle を読む関数は読み込みの最中にコードを実行し、
そのコードは自分のファイルを元の版へ書き戻せる。後で取ると、書き戻した後の値が記録に載る。
**読み込みが失敗しても記録を出す。** コードを実行させてから落とす成果物を、観測から消さない。
送出の失敗は読み込みへ波及させない。
"""

from __future__ import annotations

import functools
import importlib
from collections.abc import Callable
from typing import Any

from senda_argus_hooks.core.model_artifacts import artifact_path_of, describe_artifact
from senda_argus_hooks.core.runtime import emit_event

from .base import BaseInstrumentor, audit_guard

MODEL_LOADED_EVENT = "model.loaded"

# (モジュール, 属性, 記録に載せる関数の名前, SDK の名前)
_TARGETS: tuple[tuple[str, str, str, str], ...] = (
    ("torch", "load", "torch.load", "torch"),
    ("safetensors.torch", "load_file", "safetensors.torch.load_file", "safetensors"),
    ("safetensors.numpy", "load_file", "safetensors.numpy.load_file", "safetensors"),
    ("joblib", "load", "joblib.load", "joblib"),
)


def model_loaded_payload(
    loader: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """読み込みの引数から、記録の data.model を組み立てる。"""
    target = args[0] if args else kwargs.get("f", kwargs.get("filename"))
    path = artifact_path_of(target)
    model: dict[str, Any] = {"loader": loader}
    if path is not None:
        model.update(describe_artifact(path))
        if loader == "joblib.load" and model.get("format") == "compressed":
            model["format"] = "joblib"
    else:
        model["format"] = "unknown"
        model["source"] = "file_object"
    if loader == "torch.load":
        weights_only = kwargs.get("weights_only")
        model["weights_only"] = weights_only if isinstance(weights_only, bool) else None
    return model


class ModelLoadingInstrumentor(BaseInstrumentor):
    name = "model_loading"

    def __init__(self) -> None:
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        patched = False
        for module_name, attr, loader, sdk in _TARGETS:
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001, S112 - 任意の SDK の import 失敗は未導入として扱う
                continue
            original = getattr(module, attr, None)
            if original is None or hasattr(original, "__senda_patched__"):
                continue
            wrapped = self._wrap(original, loader, sdk)
            wrapped.__senda_patched__ = True  # type: ignore[attr-defined]
            setattr(module, attr, wrapped)
            self._patches.append((module, attr, original))
            patched = True
        return patched

    def _wrap(self, original: Callable, loader: str, sdk: str) -> Callable:
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            payload: dict[str, Any] | None = None
            with audit_guard(loader):
                payload = model_loaded_payload(loader, args, kwargs)
            source = {"component": "instrumentor", "sdk": sdk, "operation": loader}
            try:
                result = original(*args, **kwargs)
            except Exception as exc:
                with audit_guard(loader):
                    emit_event(
                        MODEL_LOADED_EVENT,
                        source=source,
                        data={"model": payload or {"loader": loader}},
                        status="error",
                        error={"type": exc.__class__.__name__, "message": str(exc)},
                    )
                raise
            with audit_guard(loader):
                emit_event(
                    MODEL_LOADED_EVENT,
                    source=source,
                    data={"model": payload or {"loader": loader}},
                    status="success",
                )
            return result

        return wrapper

    def uninstrument(self) -> bool:
        for module, attr, original in reversed(self._patches):
            setattr(module, attr, original)
        self._patches.clear()
        return True
