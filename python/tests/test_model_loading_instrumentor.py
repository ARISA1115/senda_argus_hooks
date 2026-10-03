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
