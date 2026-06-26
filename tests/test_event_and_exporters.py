import json
from pathlib import Path

from senda_argus_hooks import audit, register, shutdown


def test_custom_event_jsonl(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}])
    audit.event("custom.event", {"hello": "world"})
    shutdown()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "custom.event"
    assert event["project"] == "test"
    assert event["data"]["hello"] == "world"


def test_redaction(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], redact=True)
    audit.event("custom.event", {"api_key": "sk-12345678901234567890", "nested": {"authorization": "Bearer abc.def"}})
    shutdown()
    text = path.read_text(encoding="utf-8")
    assert "sk-123456" not in text
    assert "abc.def" not in text
    assert "***REDACTED***" in text


def test_parquet_exporter(tmp_path: Path):
    pytest = __import__("pytest")
    pytest.importorskip("pyarrow")
    out_dir = tmp_path / "parquet"
    register(project="test", exporters=[{"type": "parquet", "dir": str(out_dir), "flush_size": 2}])
    audit.event("custom.event", {"n": 1})
    audit.event("custom.event", {"n": 2})
    shutdown()
    files = list(out_dir.glob("*.parquet"))
    assert files
    import pyarrow.parquet as pq
    table = pq.read_table(files[0])
    rows = table.to_pylist()
    assert len(rows) == 2
    assert rows[0]["event_type"] == "custom.event"
