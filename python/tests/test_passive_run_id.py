"""受動計装イベントが安定した非空 run_id を持つことの回帰テスト。

span を張らない受動計装は get_run_id() も _config.run_id も None になりうる。その場合に
プロセス寿命の run_id をフォールバックで付与し、取り込み側の run_id 空ドロップと run スコープ
検知の空振りを防ぐ。span と明示 config はこのフォールバックより優先されることも固定する。
"""

import json
import os
from pathlib import Path

import pytest

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core.runtime import _process_run_id, emit_event, span_context


def _events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_passive_emit_has_stable_nonempty_run_id(tmp_path):
    path = tmp_path / "events.jsonl"
    register(project="t", exporters=[{"type": "jsonl", "path": str(path)}], auto_instrument=False)
    try:
        emit_event("llm.request", data={"model": "x"})
        emit_event("llm.request", data={"model": "y"})
    finally:
        shutdown()
    run_ids = [e.get("run_id") for e in _events(path)]
    assert run_ids and all(run_ids)  # 全イベントが非空 run_id を持つ
    assert len(set(run_ids)) == 1     # プロセス内で安定(同一 run_id)


def test_span_run_id_overrides_process_fallback(tmp_path):
    path = tmp_path / "events.jsonl"
    register(project="t", exporters=[{"type": "jsonl", "path": str(path)}], auto_instrument=False)
    try:
        with span_context("agent.turn"):
            emit_event("llm.request", data={"model": "x"})
    finally:
        shutdown()
    run_ids = [e.get("run_id") for e in _events(path)]
    assert run_ids and all(run_ids)
    assert len(set(run_ids)) == 1     # span 内は span の run_id を共有


def test_config_run_id_overrides_process_fallback(tmp_path):
    path = tmp_path / "events.jsonl"
    register(
        project="t",
        run_id="fixed-run-123",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=False,
    )
    try:
        emit_event("llm.request", data={"model": "x"})
    finally:
        shutdown()
    run_ids = [e.get("run_id") for e in _events(path)]
    assert run_ids == ["fixed-run-123"]  # 明示 config が優先


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork が無い環境")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_process_run_id_differs_across_fork():
    # import 後に fork するプリフォーク型でも、子は親と別の run_id を持つ(PID 採番)。
    parent_run_id = _process_run_id()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # 子プロセス
        os.close(read_fd)
        os.write(write_fd, _process_run_id().encode("utf-8"))
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    child_run_id = os.read(read_fd, 200).decode("utf-8")
    os.close(read_fd)
    os.waitpid(pid, 0)
    assert child_run_id
    assert child_run_id != parent_run_id
