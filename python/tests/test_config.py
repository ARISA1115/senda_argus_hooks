from __future__ import annotations

import os
from pathlib import Path

import senda_argus_hooks.config as config


def test_load_config_does_not_override_process_env(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "hooks.env"
    cfg.write_text("SENDA_ARGUS_PROJECT=file-project\nSENDA_ARGUS_ENABLED=true\n", encoding="utf-8")
    monkeypatch.setenv("SENDA_ARGUS_CONFIG", str(cfg))
    monkeypatch.setenv("SENDA_ARGUS_PROJECT", "process-project")
    monkeypatch.delenv("SENDA_ARGUS_ENABLED", raising=False)
    monkeypatch.setattr(config, "_LOADED", False)
    # Avoid host-level config influencing this test.
    monkeypatch.setattr(config, "config_paths", lambda: [cfg])
    applied = config.load_config_files()
    assert os.environ["SENDA_ARGUS_PROJECT"] == "process-project"
    assert os.environ["SENDA_ARGUS_ENABLED"] == "true"
    assert "SENDA_ARGUS_PROJECT" not in applied


def test_parse_only_senda_keys(tmp_path: Path):
    cfg = tmp_path / "hooks.env"
    cfg.write_text("OTHER_SECRET=x\nSENDA_ARGUS_PROJECT='demo'\n", encoding="utf-8")
    parsed = config._parse_env_file(cfg)
    assert parsed == {"SENDA_ARGUS_PROJECT": "demo"}
