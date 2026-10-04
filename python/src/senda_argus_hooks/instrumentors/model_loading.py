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
**記録の値は読み込んだ物から取る。** パスを開いてダイジェストを取ってから読み込みが開き直すと、
その間にパスやシンボリックリンクを差し替えられたとき、別の物の値が載る。同じ記述子を渡しても、
同じ inode を元のパスやハードリンクから上書きや切り詰めされると、ローダーは新しい内容を読む。
そのため、成果物がダイジェストの上限以下のときは1回だけ開いて全体をバイト列へ読み、そのバイト列
からダイジェストと形式を求め、同じバイト列を読み込みへ渡す。torch.load と joblib.load へは
io.BytesIO にして渡し、safetensors の load_file は device の指定が無いか cpu のとき、同じバイト列を
safetensors の load へ渡す。戻り値は元の関数と同じ形で、テンソルは cpu に置かれる。
読み込みの準備が例外で失敗したときは元の引数のまま読み込み、artifact_identity_changed を立てる。

それ以外の読み込みはパスのまま渡すしかない。対象は、device が cpu 以外の safetensors の load_file、
上限を超える成果物、mmap を指定した torch.load、mmap_mode を指定した joblib.load である。読み込みの前後の比較では、読み込みの間だけ差し替えて元へ戻す手を判別できない。そのため
artifact_identity_changed を常に立て、記録の値が読み込んだ物と一致するかを判別できないことを
示す。受け取り側はこの印を検知する側へ倒すため、これらの経路は差し替えが無くても毎回警報になる。
誤検知が増えるのはこの経路だけで、利用者は device を cpu にして後で移すか、mmap を外せば避けられる。
**読み込みが失敗しても記録を出す。** コードを実行させてから落とす成果物を、観測から消さない。
送出の失敗は読み込みへ波及させない。
"""

from __future__ import annotations

import functools
import importlib
import io
import os
from collections.abc import Callable
from typing import Any

from senda_argus_hooks.core.model_artifacts import (
    artifact_path_of,
    describe_artifact,
    describe_artifact_bytes,
    read_artifact_bytes,
)
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


def _target_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    return args[0] if args else kwargs.get("f", kwargs.get("filename"))


def _accepts_open_file(loader: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    """開いた対象を渡しても読み込みの意味が変わらない呼び出しか。mmap はパスを要る。"""
    if loader == "torch.load":
        return not kwargs.get("mmap")
    if loader == "joblib.load":
        mmap_mode = args[1] if len(args) > 1 else kwargs.get("mmap_mode")
        return mmap_mode is None
    return False


def _finish_payload(loader: str, model: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    if loader == "joblib.load" and model.get("format") == "compressed":
        model["format"] = "joblib"
    if loader == "torch.load":
        weights_only = kwargs.get("weights_only")
        model["weights_only"] = weights_only if isinstance(weights_only, bool) else None
    return model


def model_loaded_payload(
    loader: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """読み込みの引数から、記録の data.model を組み立てる。パスは開き直して読む。"""
    path = artifact_path_of(_target_of(args, kwargs))
    model: dict[str, Any] = {"loader": loader}
    if path is not None:
        model.update(describe_artifact(path))
    else:
        model["format"] = "unknown"
        model["source"] = "file_object"
    return _finish_payload(loader, model, kwargs)


# safetensors の load_file と、同じバイト列を受ける load の対応。
_SAFETENSORS_BYTES_LOADERS: dict[str, str] = {
    "safetensors.torch.load_file": "safetensors.torch",
    "safetensors.numpy.load_file": "safetensors.numpy",
}


def _cpu_device(loader: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    if loader != "safetensors.torch.load_file":
        return True
    device = args[1] if len(args) > 1 else kwargs.get("device", "cpu")
    return isinstance(device, str) and device == "cpu"


class _Prepared:
    """1回の読み込みの記録と、読み込みへ渡す関数と引数。"""

    def __init__(self, loader: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.loader = loader
        self.args = args
        self.kwargs = kwargs
        self.call: Callable | None = None
        self.payload: dict[str, Any] = {"loader": loader}

    def prepare(self) -> None:
        original_args, original_kwargs = self.args, self.kwargs
        try:
            self._prepare()
        except Exception:
            # 記録の値が読み込む物と一致するかを判別できない。元の引数のまま読み込ませ、印を立てる。
            self.args, self.kwargs, self.call = original_args, original_kwargs, None
            self.payload = {"loader": self.loader, "artifact_identity_changed": True}
            path = artifact_path_of(_target_of(original_args, original_kwargs))
            if path is not None:
                self.payload["artifact_path"] = os.path.abspath(path)
            raise

    def _prepare(self) -> None:
        loader, args, kwargs = self.loader, self.args, self.kwargs
        path = artifact_path_of(_target_of(args, kwargs))
        if path is None:
            self.payload = model_loaded_payload(loader, args, kwargs)
            return
        if _accepts_open_file(loader, args, kwargs) or (
            loader in _SAFETENSORS_BYTES_LOADERS and _cpu_device(loader, args, kwargs)
        ):
            data = _read_once(path)
            if data is not None:
                self._use_bytes(path, data)
                return
        # パスのまま渡すしかない。読み込んだ物が記録の物かを判別できない。
        self.payload = model_loaded_payload(loader, args, kwargs)
        self.payload["artifact_identity_changed"] = True

    def _use_bytes(self, path: str, data: bytes) -> None:
        model = {"loader": self.loader, **describe_artifact_bytes(data, path)}
        if self.loader in _SAFETENSORS_BYTES_LOADERS:
            self.call = importlib.import_module(_SAFETENSORS_BYTES_LOADERS[self.loader]).load
            self.args, self.kwargs = (data,), {}
        else:
            stream = io.BytesIO(data)
            if self.args:
                self.args = (stream, *self.args[1:])
            else:
                key = "f" if "f" in self.kwargs else "filename"
                self.kwargs = {**self.kwargs, key: stream}
        self.payload = _finish_payload(self.loader, model, self.kwargs)


def _read_once(path: str) -> bytes | None:
    """成果物を1回だけ開いて上限まで読む。開けないか上限を超えれば None。"""
    try:
        with open(path, "rb") as fh:
            return read_artifact_bytes(fh)
    except OSError:
        return None


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
            prepared = _Prepared(loader, args, kwargs)
            with audit_guard(loader):
                prepared.prepare()
            call, call_args, call_kwargs = prepared.call or original, prepared.args, prepared.kwargs
            source = {"component": "instrumentor", "sdk": sdk, "operation": loader}
            try:
                result = call(*call_args, **call_kwargs)
            except Exception as exc:
                with audit_guard(loader):
                    emit_event(
                        MODEL_LOADED_EVENT,
                        source=source,
                        data={"model": prepared.payload},
                        status="error",
                        error={"type": exc.__class__.__name__, "message": str(exc)},
                    )
                raise
            with audit_guard(loader):
                emit_event(
                    MODEL_LOADED_EVENT,
                    source=source,
                    data={"model": prepared.payload},
                    status="success",
                )
            return result

        return wrapper

    def uninstrument(self) -> bool:
        for module, attr, original in reversed(self._patches):
            setattr(module, attr, original)
        self._patches.clear()
        return True
