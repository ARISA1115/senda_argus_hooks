from pathlib import Path

from senda_argus_hooks import audit, register, shutdown
from senda_argus_hooks.cli.main import main


def make_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "events.jsonl"
    register(project="cli-test", exporters=[{"type": "jsonl", "path": str(path)}])
    audit.event("custom.event", {"x": 1})
    shutdown()
    return path


def test_cli_validate(tmp_path, capsys):
    path = make_jsonl(tmp_path)
    code = main(["validate", str(path)])
    captured = capsys.readouterr()
    assert code == 0
    assert '"valid": true' in captured.out


def test_cli_inspect_summary(tmp_path, capsys):
    path = make_jsonl(tmp_path)
    code = main(["inspect", str(path), "--summary"])
    captured = capsys.readouterr()
    assert code == 0
    assert "custom.event" in captured.out


def test_cli_convert_to_parquet(tmp_path):
    pytest = __import__("pytest")
    pytest.importorskip("pyarrow")
    path = make_jsonl(tmp_path)
    out_dir = tmp_path / "pq"
    code = main(["convert", str(path), "--to", "parquet", "--out", str(out_dir)])
    assert code == 0
    assert list(out_dir.glob("*.parquet"))
