"""ローカルのモデルの読み込みが model.loaded として届くことを確かめる。

形式は成果物の先頭の数バイトから判別し、拡張子からは決めない。ダイジェストは成果物全体の
SHA-256 で、Argus の改ざんの規則の基準線の値になる。読み込みの包みは実物のパッケージで確かめる。
"""

from __future__ import annotations

import hashlib
import json
import pickle
import struct
import zipfile
from pathlib import Path

import pytest

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core.model_artifacts import (
    artifact_digest,
    describe_artifact,
    sniff_format,
)


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestSniffFormat:
    def test_pickle_is_detected_from_the_bytes_even_with_a_safe_suffix(self, tmp_path):
        path = tmp_path / "weights.safetensors"
        path.write_bytes(pickle.dumps({"w": [1, 2]}, protocol=4))
        assert sniff_format(str(path)) == "pickle"

    def test_safetensors_header(self, tmp_path):
        header = b'{"__metadata__":{}}'
        path = tmp_path / "model.bin"
        path.write_bytes(struct.pack("<Q", len(header)) + header)
        assert sniff_format(str(path)) == "safetensors"

    def test_gguf_magic(self, tmp_path):
        path = tmp_path / "m"
        path.write_bytes(b"GGUF\x03\x00\x00\x00rest")
        assert sniff_format(str(path)) == "gguf"

    def test_zip_holding_a_pickle_is_pytorch_zip(self, tmp_path):
        path = tmp_path / "model.pt"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("archive/data.pkl", pickle.dumps([1]))
        assert sniff_format(str(path)) == "pytorch_zip"

    def test_unrecognized_bytes_are_unknown(self, tmp_path):
        path = tmp_path / "model.pkl"
        path.write_bytes(b"plain text file")
        assert sniff_format(str(path)) == "unknown"


class TestDigest:
    def test_digest_is_sha256_of_the_whole_file(self, tmp_path):
        path = tmp_path / "a.bin"
        body = b"x" * 10_000
        path.write_bytes(body)
        digest, truncated = artifact_digest(str(path))
        assert digest == "sha256:" + hashlib.sha256(body).hexdigest()
        assert truncated is False

    def test_rewritten_file_gets_a_new_digest(self, tmp_path):
        path = tmp_path / "a.bin"
        path.write_bytes(b"first")
        first, _ = artifact_digest(str(path))
        path.write_bytes(b"second-longer")
        second, _ = artifact_digest(str(path))
        assert first != second

    def test_same_size_rewrite_with_restored_mtime_gets_a_new_digest(self, tmp_path):
        """大きさと更新時刻を元へ戻した差し替えも、新しいダイジェストになる。"""
        import os

        path = tmp_path / "a.bin"
        path.write_bytes(b"A" * 100)
        before = os.stat(path)
        first, _ = artifact_digest(str(path))
        path.write_bytes(b"B" * 100)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        second, _ = artifact_digest(str(path))
        assert first != second

    def test_size_over_the_limit_is_marked_truncated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SENDA_ARGUS_MODEL_DIGEST_MAX_BYTES", "4")
        path = tmp_path / "a.bin"
        path.write_bytes(b"12345")
        described = describe_artifact(str(path))
        assert "artifact_hash" not in described
        assert described["digest_truncated"] is True


def _register(path: Path):
    return register(
        project="test-model-loading",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_openai=False,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_ollama=False,
        instrument_bedrock=False,
        instrument_vertexai=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        instrument_openai_agents=False,
    )


def test_joblib_load_emits_model_loaded_with_format_and_digest(tmp_path):
    joblib = pytest.importorskip("joblib")
    artifact = tmp_path / "clf.joblib"
    joblib.dump({"coef": [1.0, 2.0]}, artifact, compress=3)
    events_path = tmp_path / "events.jsonl"
    result = _register(events_path)
    try:
        assert result["instrumentors"]["model_loading"] is True
        assert joblib.load(artifact) == {"coef": [1.0, 2.0]}
    finally:
        shutdown()
    events = [e for e in _read_events(events_path) if e["event_type"] == "model.loaded"]
    assert len(events) == 1
    model = events[0]["data"]["model"]
    assert model["loader"] == "joblib.load"
    assert model["format"] == "joblib"
    assert model["artifact_path"] == str(artifact.resolve())
    assert model["artifact_hash"] == "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_safetensors_numpy_load_file_emits_model_loaded(tmp_path):
    pytest.importorskip("numpy")
    st_numpy = pytest.importorskip("safetensors.numpy")
    import numpy as np

    artifact = tmp_path / "w.safetensors"
    st_numpy.save_file({"w": np.zeros((2, 2), dtype=np.float32)}, str(artifact))
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        import safetensors.numpy as reloaded

        reloaded.load_file(str(artifact))
    finally:
        shutdown()
    events = [e for e in _read_events(events_path) if e["event_type"] == "model.loaded"]
    assert len(events) == 1
    assert events[0]["data"]["model"]["format"] == "safetensors"
    assert events[0]["data"]["model"]["loader"] == "safetensors.numpy.load_file"


def test_loader_failure_is_recorded_and_reraised(tmp_path):
    joblib = pytest.importorskip("joblib")
    artifact = tmp_path / "broken.joblib"
    artifact.write_bytes(b"\x80\x04\x95garbage-not-a-pickle")
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        with pytest.raises(Exception):  # noqa: B017 - 失敗の種類は joblib の版で変わる
            joblib.load(artifact)
    finally:
        shutdown()
    events = [e for e in _read_events(events_path) if e["event_type"] == "model.loaded"]
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert events[0]["data"]["model"]["artifact_hash"] == (
        "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    )


def test_digest_is_taken_before_the_loader_runs(tmp_path, monkeypatch):
    """読み込みの最中にファイルを書き戻しても、読み込んだ時点の中身のダイジェストが載る。"""
    joblib = pytest.importorskip("joblib")
    artifact = tmp_path / "m.joblib"
    joblib.dump({"v": 1}, artifact)
    loaded_bytes = artifact.read_bytes()
    original = joblib.load

    def restoring_load(path, *a, **k):
        value = original(path, *a, **k)
        artifact.write_bytes(b"restored-original-version")
        return value

    monkeypatch.setattr(joblib, "load", restoring_load)
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        joblib.load(artifact)
    finally:
        shutdown()
    events = [e for e in _read_events(events_path) if e["event_type"] == "model.loaded"]
    assert events[0]["data"]["model"]["artifact_hash"] == (
        "sha256:" + hashlib.sha256(loaded_bytes).hexdigest()
    )


def _swap_in(path: Path, body: bytes) -> None:
    """別のプロセスがする原子的な差し替えと同じく、新しいファイルを rename で置く。"""
    staged = path.with_name(path.name + ".staged")
    staged.write_bytes(body)
    staged.replace(path)


def _model_events(path: Path):
    return [e for e in _read_events(path) if e["event_type"] == "model.loaded"]


def test_joblib_swap_after_digest_loads_the_digested_artifact(tmp_path, monkeypatch):
    """ダイジェストの後と読み込みの前の間に差し替えても、読み込むのはダイジェストを取った物。"""
    joblib = pytest.importorskip("joblib")
    artifact = tmp_path / "m.joblib"
    joblib.dump({"v": "digested"}, artifact)
    digested_bytes = artifact.read_bytes()
    swapped = tmp_path / "other.joblib"
    joblib.dump({"v": "swapped"}, swapped)
    _swap_after_bytes_digest(monkeypatch, artifact, swapped.read_bytes())
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        value = joblib.load(artifact)
    finally:
        shutdown()
    assert value == {"v": "digested"}
    model = _model_events(events_path)[0]["data"]["model"]
    assert model["artifact_hash"] == "sha256:" + hashlib.sha256(digested_bytes).hexdigest()
    assert "artifact_identity_changed" not in model


def test_torch_swap_after_digest_loads_the_digested_artifact(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    artifact = tmp_path / "m.pt"
    torch.save({"w": torch.tensor([1.0])}, artifact)
    digested_bytes = artifact.read_bytes()
    swapped = tmp_path / "other.pt"
    torch.save({"w": torch.tensor([9.0])}, swapped)
    _swap_after_bytes_digest(monkeypatch, artifact, swapped.read_bytes())
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        value = torch.load(artifact, weights_only=True)
    finally:
        shutdown()
    assert value["w"].item() == 1.0
    model = _model_events(events_path)[0]["data"]["model"]
    assert model["format"] == "pytorch_zip"
    assert model["artifact_hash"] == "sha256:" + hashlib.sha256(digested_bytes).hexdigest()


def _safetensors_torch_pair(tmp_path):
    torch = pytest.importorskip("torch")
    st_torch = pytest.importorskip("safetensors.torch")
    artifact = tmp_path / "w.safetensors"
    st_torch.save_file({"w": torch.zeros(2), "b": torch.arange(3)}, str(artifact))
    other = tmp_path / "other.safetensors"
    st_torch.save_file({"w": torch.ones(2), "b": torch.arange(3)}, str(other))
    return torch, artifact, other


def _swap_after_bytes_digest(monkeypatch, artifact, body):
    from senda_argus_hooks.instrumentors import model_loading

    real = model_loading.describe_artifact_bytes

    def describe_then_swap(data, path):
        out = real(data, path)
        _swap_in(artifact, body)
        return out

    monkeypatch.setattr(model_loading, "describe_artifact_bytes", describe_then_swap)


def test_safetensors_torch_swap_after_digest_loads_the_digested_bytes(tmp_path, monkeypatch):
    """ダイジェストを取ったバイト列そのものを読み込むため、後の差し替えは読み込みに届かない。"""
    torch, artifact, other = _safetensors_torch_pair(tmp_path)
    import safetensors.torch as st_torch

    expected = st_torch.load_file(str(artifact))
    digested = artifact.read_bytes()
    _swap_after_bytes_digest(monkeypatch, artifact, other.read_bytes())
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        value = st_torch.load_file(str(artifact))
    finally:
        shutdown()
    assert isinstance(value, dict) and set(value) == set(expected)
    for name, tensor in value.items():
        assert isinstance(tensor, torch.Tensor)
        assert tensor.device == expected[name].device == torch.device("cpu")
        assert tensor.dtype == expected[name].dtype
        assert torch.equal(tensor, expected[name])
    model = _model_events(events_path)[0]["data"]["model"]
    assert model["artifact_hash"] == "sha256:" + hashlib.sha256(digested).hexdigest()
    assert model["format"] == "safetensors"
    assert "artifact_identity_changed" not in model


def test_safetensors_torch_explicit_cpu_device_reads_bytes(tmp_path):
    torch, artifact, _ = _safetensors_torch_pair(tmp_path)
    import safetensors.torch as st_torch

    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        value = st_torch.load_file(str(artifact), device="cpu")
    finally:
        shutdown()
    assert value["w"].device == torch.device("cpu")
    assert "artifact_identity_changed" not in _model_events(events_path)[0]["data"]["model"]


def test_safetensors_numpy_swap_after_digest_loads_the_digested_bytes(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    st_numpy = pytest.importorskip("safetensors.numpy")
    artifact = tmp_path / "w.safetensors"
    st_numpy.save_file({"w": np.zeros((2,), dtype=np.float32)}, str(artifact))
    other = tmp_path / "other.safetensors"
    st_numpy.save_file({"w": np.ones((2,), dtype=np.float32)}, str(other))
    _swap_after_bytes_digest(monkeypatch, artifact, other.read_bytes())
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        import safetensors.numpy as reloaded

        value = reloaded.load_file(str(artifact))
    finally:
        shutdown()
    assert isinstance(value["w"], np.ndarray) and value["w"].tolist() == [0.0, 0.0]
    assert "artifact_identity_changed" not in _model_events(events_path)[0]["data"]["model"]


# 次の経路はパスのまま読み込むしかなく、差し替えが無くても印が立つ。受け取り側では毎回警報になる。
# 誤検知が増えるのはこの経路だけで、各テストは差し替えを起こさない条件だけで印を確認する。


def test_safetensors_non_cpu_device_is_marked_without_any_swap(tmp_path):
    _, artifact, _ = _safetensors_torch_pair(tmp_path)
    import safetensors.torch as st_torch

    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        with pytest.raises(Exception):  # noqa: B017 - cuda の無い環境では読み込みが失敗する
            st_torch.load_file(str(artifact), device="cuda:0")
    finally:
        shutdown()
    assert _model_events(events_path)[0]["data"]["model"]["artifact_identity_changed"] is True


def test_safetensors_over_the_digest_limit_is_marked_without_any_swap(tmp_path, monkeypatch):
    _, artifact, _ = _safetensors_torch_pair(tmp_path)
    import safetensors.torch as st_torch

    monkeypatch.setenv("SENDA_ARGUS_MODEL_DIGEST_MAX_BYTES", "8")
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        st_torch.load_file(str(artifact))
    finally:
        shutdown()
    model = _model_events(events_path)[0]["data"]["model"]
    assert model["artifact_identity_changed"] is True
    assert model["digest_truncated"] is True


def test_torch_mmap_is_marked_without_any_swap(tmp_path):
    torch = pytest.importorskip("torch")
    artifact = tmp_path / "m.pt"
    torch.save({"w": torch.tensor([1.0])}, artifact)
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        torch.load(artifact, mmap=True, weights_only=True)
    finally:
        shutdown()
    assert _model_events(events_path)[0]["data"]["model"]["artifact_identity_changed"] is True


def test_joblib_mmap_mode_is_marked_without_any_swap(tmp_path):
    joblib = pytest.importorskip("joblib")
    np = pytest.importorskip("numpy")
    artifact = tmp_path / "a.joblib"
    joblib.dump(np.zeros(4), artifact)
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        joblib.load(artifact, mmap_mode="r")
    finally:
        shutdown()
    assert _model_events(events_path)[0]["data"]["model"]["artifact_identity_changed"] is True


def test_joblib_without_mmap_is_not_marked(tmp_path):
    joblib = pytest.importorskip("joblib")
    artifact = tmp_path / "a.joblib"
    joblib.dump({"v": 1}, artifact)
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        joblib.load(artifact)
    finally:
        shutdown()
    assert "artifact_identity_changed" not in _model_events(events_path)[0]["data"]["model"]


def _overwrite_in_place(path: Path, body: bytes) -> None:
    """同じ inode を元のパスから上書きする。開いた記述子から読む側にも新しい内容が見える。"""
    with open(path, "r+b") as fh:
        fh.write(body)
        fh.truncate()


def _overwrite_after_bytes_digest(monkeypatch, artifact, body):
    from senda_argus_hooks.instrumentors import model_loading

    real = model_loading.describe_artifact_bytes

    def describe_then_overwrite(data, path):
        out = real(data, path)
        _overwrite_in_place(artifact, body)
        return out

    monkeypatch.setattr(model_loading, "describe_artifact_bytes", describe_then_overwrite)


def test_torch_in_place_overwrite_after_digest_loads_the_digested_bytes(tmp_path, monkeypatch):
    """同じ inode の上書きでも、読み込むのはダイジェストを取ったバイト列。"""
    torch = pytest.importorskip("torch")
    artifact = tmp_path / "m.pt"
    torch.save({"w": torch.tensor([1.0])}, artifact)
    digested = artifact.read_bytes()
    other = tmp_path / "other.pt"
    torch.save({"w": torch.tensor([9.0])}, other)
    _overwrite_after_bytes_digest(monkeypatch, artifact, other.read_bytes())
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        value = torch.load(artifact, weights_only=True)
    finally:
        shutdown()
    assert artifact.read_bytes() == other.read_bytes()
    assert value["w"].item() == 1.0
    model = _model_events(events_path)[0]["data"]["model"]
    assert model["artifact_hash"] == "sha256:" + hashlib.sha256(digested).hexdigest()


def test_joblib_in_place_overwrite_after_digest_loads_the_digested_bytes(tmp_path, monkeypatch):
    joblib = pytest.importorskip("joblib")
    artifact = tmp_path / "m.joblib"
    joblib.dump({"v": "digested"}, artifact, compress=3)
    digested = artifact.read_bytes()
    other = tmp_path / "other.joblib"
    joblib.dump({"v": "overwritten"}, other, compress=3)
    _overwrite_after_bytes_digest(monkeypatch, artifact, other.read_bytes())
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        value = joblib.load(artifact)
    finally:
        shutdown()
    assert value == {"v": "digested"}
    model = _model_events(events_path)[0]["data"]["model"]
    assert model["artifact_hash"] == "sha256:" + hashlib.sha256(digested).hexdigest()
    assert model["format"] == "joblib"


def test_bytes_stream_returns_the_same_value_as_the_path(tmp_path):
    """io.BytesIO を渡した読み込みは、パスを渡した読み込みと同じ値を返す。"""
    torch = pytest.importorskip("torch")
    joblib = pytest.importorskip("joblib")
    pt = tmp_path / "m.pt"
    torch.save({"w": torch.arange(4, dtype=torch.float16)}, pt)
    jl = tmp_path / "m.joblib"
    joblib.dump({"a": [1, 2], "b": "x"}, jl, compress=3)
    expected_pt = torch.load(pt, weights_only=True)
    expected_jl = joblib.load(jl)
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        got_pt = torch.load(pt, weights_only=True)
        got_jl = joblib.load(jl)
    finally:
        shutdown()
    assert got_pt["w"].dtype == expected_pt["w"].dtype
    assert got_pt["w"].device == expected_pt["w"].device
    assert torch.equal(got_pt["w"], expected_pt["w"])
    assert got_jl == expected_jl
    models = [e["data"]["model"] for e in _model_events(events_path)]
    assert len(models) == 2
    assert all("artifact_identity_changed" not in m for m in models)


def test_a_huge_limit_does_not_allocate_the_limit_for_a_small_file(tmp_path, monkeypatch):
    """上限が実際の大きさより遥かに大きくても、小さな成果物は読めてダイジェストが載る。"""
    joblib = pytest.importorskip("joblib")
    monkeypatch.setenv("SENDA_ARGUS_MODEL_DIGEST_MAX_BYTES", str(2**62))
    artifact = tmp_path / "m.joblib"
    joblib.dump({"v": 1}, artifact)
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        joblib.load(artifact)
    finally:
        shutdown()
    model = _model_events(events_path)[0]["data"]["model"]
    assert model["artifact_hash"] == "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert "artifact_identity_changed" not in model


def test_a_failed_snapshot_is_marked_and_the_load_still_runs(tmp_path, monkeypatch):
    joblib = pytest.importorskip("joblib")
    from senda_argus_hooks.instrumentors import model_loading

    def failing(_fh):
        raise MemoryError("snapshot")

    monkeypatch.setattr(model_loading, "read_artifact_bytes", failing)
    artifact = tmp_path / "m.joblib"
    joblib.dump({"v": 1}, artifact)
    events_path = tmp_path / "events.jsonl"
    _register(events_path)
    try:
        assert joblib.load(artifact) == {"v": 1}
    finally:
        shutdown()
    model = _model_events(events_path)[0]["data"]["model"]
    assert model["artifact_identity_changed"] is True
    assert model["artifact_path"] == str(artifact.resolve())
