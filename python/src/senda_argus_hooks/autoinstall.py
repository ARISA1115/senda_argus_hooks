from __future__ import annotations

import json
import os
import site
import sys
from pathlib import Path
from typing import Any

PTH_FILENAME = "senda_argus_autohook.pth"
PTH_CONTENT = "import senda_argus_hooks.autohook as _senda_argus_autohook; _senda_argus_autohook.bootstrap()\n"


def _in_venv() -> bool:
    return getattr(sys, "base_prefix", sys.prefix) != sys.prefix or hasattr(sys, "real_prefix")


def _system_site_dirs() -> list[Path]:
    result: list[Path] = []
    try:
        result.extend(Path(p) for p in site.getsitepackages())
    except Exception:
        pass
    # Fallback for unusual Python distributions.
    for p in sys.path:
        if p and (p.endswith("site-packages") or p.endswith("dist-packages")):
            path = Path(p)
            if path not in result:
                result.append(path)
    return result


def resolve_install_dir(scope: str = "auto", target: str | None = None) -> Path:
    if target:
        return Path(target).expanduser().resolve()
    if scope not in {"auto", "user", "system"}:
        raise ValueError("scope must be one of: auto, user, system")
    if scope == "user":
        return Path(site.getusersitepackages()).expanduser().resolve()
    system_dirs = _system_site_dirs()
    if scope == "system":
        if not system_dirs:
            raise RuntimeError("Could not determine system site-packages for this Python interpreter.")
        return system_dirs[0].resolve()
    if _in_venv() and system_dirs:
        return system_dirs[0].resolve()
    return Path(site.getusersitepackages()).expanduser().resolve()


def install(*, scope: str = "auto", target: str | None = None, force: bool = False) -> dict[str, Any]:
    install_dir = resolve_install_dir(scope=scope, target=target)
    install_dir.mkdir(parents=True, exist_ok=True)
    pth_path = install_dir / PTH_FILENAME
    if pth_path.exists() and pth_path.read_text(encoding="utf-8") != PTH_CONTENT and not force:
        raise RuntimeError(f"Refusing to overwrite existing non-Senda file: {pth_path}. Use --force to replace it.")
    pth_path.write_text(PTH_CONTENT, encoding="utf-8")
    return {
        "installed": True,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "scope": scope,
        "virtualenv": _in_venv(),
        "site_packages": str(install_dir),
        "pth": str(pth_path),
        "enabled_by_default": True,
    }


def _candidate_paths(target: str | None = None) -> list[Path]:
    if target:
        return [Path(target).expanduser().resolve() / PTH_FILENAME]
    candidates: list[Path] = []
    for path in _system_site_dirs() + [Path(site.getusersitepackages())]:
        pth = path.expanduser().resolve() / PTH_FILENAME
        if pth not in candidates:
            candidates.append(pth)
    return candidates


def status(*, target: str | None = None) -> dict[str, Any]:
    entries = []
    for pth in _candidate_paths(target):
        exists = pth.exists()
        content_ok = False
        if exists:
            try:
                content_ok = pth.read_text(encoding="utf-8") == PTH_CONTENT
            except Exception:
                content_ok = False
        entries.append({"path": str(pth), "exists": exists, "managed": content_ok})
    installed = any(entry["exists"] and entry["managed"] for entry in entries)
    return {
        "installed": installed,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "virtualenv": _in_venv(),
        "enabled_env": os.getenv("SENDA_ARGUS_ENABLED", "true"),
        "entries": entries,
    }


def uninstall(*, target: str | None = None, force: bool = False) -> dict[str, Any]:
    removed: list[str] = []
    skipped: list[str] = []
    for pth in _candidate_paths(target):
        if not pth.exists():
            continue
        managed = False
        try:
            managed = pth.read_text(encoding="utf-8") == PTH_CONTENT
        except Exception:
            managed = False
        if not managed and not force:
            skipped.append(str(pth))
            continue
        pth.unlink()
        removed.append(str(pth))
    return {"installed": False, "removed": removed, "skipped": skipped}


def print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
