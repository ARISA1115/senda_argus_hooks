"""受動計装イベントが安定した非空 run_id を持つことの回帰テスト。

span を張らない受動計装は get_run_id() も _config.run_id も None になりうる。その場合に
プロセス寿命の run_id をフォールバックで付与し、取り込み側の run_id 空ドロップと run スコープ
検知の空振りを防ぐ。span と明示 config はこのフォールバックより優先されることも固定する。
"""

import json
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core.runtime import emit_event, span_context


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
