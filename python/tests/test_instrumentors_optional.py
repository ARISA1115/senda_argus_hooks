from senda_argus_hooks import register, shutdown


def test_auto_instrument_missing_optional_sdks_does_not_fail(tmp_path):
    path = tmp_path / "events.jsonl"
    result = register(
        project="test",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
    )
    assert "instrumentors" in result
    shutdown()
