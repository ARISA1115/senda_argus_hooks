from __future__ import annotations

import os
from pathlib import Path

_LOADED = False


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return values
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key.startswith("SENDA_ARGUS_"):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def config_paths() -> list[Path]:
    paths: list[Path] = [Path("/etc/senda-argus/hooks.env")]
    try:
        base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
        paths.append(base / "senda-argus" / "hooks.env")
    except Exception:
        pass
    explicit = os.getenv("SENDA_ARGUS_CONFIG")
    if explicit:
        paths.append(Path(explicit).expanduser())
    # preserve order, explicit file has highest file-level precedence
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def load_config_files() -> dict[str, str]:
    """Load Senda config files without overriding already-set process env vars."""
    global _LOADED
    if _LOADED:
        return {}
    merged: dict[str, str] = {}
    for path in config_paths():
        merged.update(_parse_env_file(path))
    applied: dict[str, str] = {}
    for key, value in merged.items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    _LOADED = True
    return applied
