from __future__ import annotations

import os
import sys
from pathlib import Path

from senda_argus_hooks.zerocode import discover_targets, inspect_python, write_config


def test_inspect_current_python():
    target = inspect_python(sys.executable, sources=["test"])
    assert target is not None
    assert target.supported is True
    assert target.primary_site_packages
    assert "test" in target.source


def test_discover_includes_installer_python(tmp_path: Path):
    targets = discover_targets(scan_roots=[str(tmp_path)], include_processes=False)
    assert targets
    assert any(Path(t.prefix).resolve() == Path(sys.prefix).resolve() for t in targets)


def test_write_config_permissions_and_values(tmp_path: Path):
    path = tmp_path / "hooks.env"
    result = write_config({"SENDA_ARGUS_PROJECT": "demo", "SENDA_ARGUS_ENDPOINT": "http://argus:8000"}, path=str(path))
    text = path.read_text(encoding="utf-8")
    assert "SENDA_ARGUS_PROJECT=demo" in text
    assert "SENDA_ARGUS_ENDPOINT=http://argus:8000" in text
    assert result["mode"] == "0600"
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
