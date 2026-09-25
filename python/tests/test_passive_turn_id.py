"""受動計装が llm.request の turn 境界ごとに一意な turn_id を採番することの確認。

固定 run_id 運用でも、正当な別プロンプトの llm.request は別 turn_id を持つ。取り込み側の
LLM_MESSAGES_HASH_MISMATCH は (run_id, turn_id) で messages_hash を比較するため、これにより
プロンプトごとに別バケットへ分離され、正当な複数プロンプトで誤発火しない。明示 turn_id は
フォールバック採番より優先し、同一 turn_id で messages_hash が変わる真の改竄検知は不変で残る。
"""

import json
import sys
import types
from pathlib import Path

from senda_argus_hooks import register, shutdown


def _read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.usage = payload.get("usage")

    def model_dump(self):
        return self.payload


def _fake_openai():
    class Completions:
        def create(self, *args, **kwargs):
            return _Response({"id": "chatcmpl_fake", "model": kwargs.get("model")})

    fake = types.ModuleType("openai")
    fake.resources = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(Completions=Completions)),
        responses=types.SimpleNamespace(Responses=type("Responses", (), {"create": lambda self, *a, **k: _Response({})})),
        embeddings=types.SimpleNamespace(Embeddings=type("Embeddings", (), {"create": lambda self, *a, **k: _Response({})})),
    )
    return fake, Completions


def _register_openai(path: Path, monkeypatch, **extra):
    fake, Completions = _fake_openai()
    monkeypatch.setitem(sys.modules, "openai", fake)
    register(
        project="test-passive-turn-id",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_ollama=False,
        instrument_bedrock=False,
        instrument_vertexai=False,
        instrument_mcp=False,
        instrument_argus_sdk=False,
        instrument_openai_agents=False,
        **extra,
    )
    return Completions()


def _llm_requests(path: Path):
    return [e for e in _read_events(path) if e.get("event_type") == "llm.request"]


def test_fixed_run_id_distinct_prompts_get_distinct_turn_ids(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    client = _register_openai(path, monkeypatch, run_id="run_fixed")
    try:
        client.create(model="gpt-fake", messages=[{"role": "user", "content": "prompt one"}])
        client.create(model="gpt-fake", messages=[{"role": "user", "content": "prompt two"}])
    finally:
        shutdown()

    reqs = _llm_requests(path)
    assert len(reqs) == 2
    # run_id は固定運用のとおり両イベントで同一。
    assert reqs[0]["run_id"] == "run_fixed"
    assert reqs[1]["run_id"] == "run_fixed"
    # turn_id は turn 境界ごとに採番され、非空かつ別プロンプトで相異なる。
    assert reqs[0]["turn_id"]
    assert reqs[1]["turn_id"]
    assert reqs[0]["turn_id"] != reqs[1]["turn_id"]


def test_explicit_config_turn_id_takes_precedence_over_fallback(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    client = _register_openai(path, monkeypatch, run_id="run_fixed", turn_id="turn_explicit")
    try:
        client.create(model="gpt-fake", messages=[{"role": "user", "content": "prompt one"}])
        client.create(model="gpt-fake", messages=[{"role": "user", "content": "prompt two"}])
    finally:
        shutdown()

    reqs = _llm_requests(path)
    assert len(reqs) == 2
    # 明示 config.turn_id はフォールバック採番より優先し、同一 turn を共有する。
    # 同一 (run_id, turn_id) で messages_hash が変わる真の改竄検知はこの経路で不変に残る。
    assert reqs[0]["turn_id"] == "turn_explicit"
    assert reqs[1]["turn_id"] == "turn_explicit"


def test_non_llm_events_keep_empty_turn_id(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    client = _register_openai(path, monkeypatch, run_id="run_fixed")
    try:
        # tools を渡し selected を返さないため agent.decision は出ない。
        # llm.request 以外は turn_id を採番しない (照合キーに使うのは llm.request のみ)。
        client.create(model="gpt-fake", messages=[{"role": "user", "content": "hi"}])
    finally:
        shutdown()

    for event in _read_events(path):
        if event.get("event_type") != "llm.request":
            assert not event.get("turn_id")
