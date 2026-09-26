from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from senda_argus_hooks.autoinstall import PTH_CONTENT, PTH_FILENAME

MIN_PYTHON = (3, 10)
DEFAULT_SCAN_ROOTS = ("/opt", "/srv", "/app", "/var/www", "/home")
CONFIG_KEYS = (
    "SENDA_ARGUS_ENABLED",
    "SENDA_ARGUS_EXPORTERS",
    "SENDA_ARGUS_EXPORTER",
    "SENDA_ARGUS_ENDPOINT",
    "SENDA_ARGUS_API_KEY",
    "SENDA_ARGUS_PROJECT",
    "SENDA_ARGUS_ENVIRONMENT",
    "SENDA_ARGUS_JSONL_PATH",
    "SENDA_ARGUS_CAPTURE_PROMPT",
    "SENDA_ARGUS_CAPTURE_RESPONSE",
    "SENDA_ARGUS_CAPTURE_ARGUMENTS",
    "SENDA_ARGUS_CAPTURE_RESULT",
    "SENDA_ARGUS_CAPTURE_HASH",
    "SENDA_ARGUS_REDACT",
)


@dataclass(frozen=True)
class PythonTarget:
    executable: str
    version: str
    version_info: tuple[int, int, int]
    prefix: str
    base_prefix: str
    site_packages: tuple[str, ...]
    user_site: str | None
    virtualenv: bool
    source: tuple[str, ...]
    running_pids: tuple[int, ...] = ()

    @property
    def supported(self) -> bool:
        return self.version_info[:2] >= MIN_PYTHON

    @property
    def primary_site_packages(self) -> str | None:
        if self.virtualenv and self.site_packages:
            return self.site_packages[0]
        for item in self.site_packages:
            if item.startswith(self.prefix):
                return item
        return self.site_packages[0] if self.site_packages else self.user_site

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["supported"] = self.supported
        data["primary_site_packages"] = self.primary_site_packages
        return data


def _run(cmd: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)


def _candidate_from_path(path: str | os.PathLike[str] | None) -> str | None:
    if not path:
        return None
    try:
        p = Path(path).expanduser()
        if p.exists() and p.is_file():
            return str(p.absolute())
    except Exception:
        return None
    return None


def _path_candidates() -> dict[str, set[str]]:
    candidates: dict[str, set[str]] = {}

    def add(path: str | None, source: str) -> None:
        resolved = _candidate_from_path(path)
        if resolved:
            candidates.setdefault(resolved, set()).add(source)

    add(sys.executable, "installer")
    for name in ("python", "python3", "python3.10", "python3.11", "python3.12", "python3.13", "python3.14"):
        add(shutil.which(name), "PATH")
    return candidates


def _proc_candidates() -> tuple[dict[str, set[str]], dict[str, set[int]]]:
    candidates: dict[str, set[str]] = {}
    pids_by_exe: dict[str, set[int]] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return candidates, pids_by_exe

    def add(path: str | None, pid: int, source: str) -> None:
        resolved = _candidate_from_path(path)
        if not resolved:
            return
        candidates.setdefault(resolved, set()).add(source)
        pids_by_exe.setdefault(resolved, set()).add(pid)

    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        pid = int(item.name)
        try:
            raw = (item / "cmdline").read_bytes()
            args = [part.decode(errors="ignore") for part in raw.split(b"\0") if part]
        except Exception:
            args = []
        first = args[0] if args else None
        if first and "python" in Path(first).name.lower():
            add(first, pid, "running-process")
            try:
                exe_link = os.readlink(item / "exe")
                add(exe_link, pid, "running-process-exe")
            except Exception:
                pass
        try:
            environ_raw = (item / "environ").read_bytes()
            env = {}
            for pair in environ_raw.split(b"\0"):
                if b"=" not in pair:
                    continue
                key, value = pair.split(b"=", 1)
                env[key.decode(errors="ignore")] = value.decode(errors="ignore")
            venv = env.get("VIRTUAL_ENV")
            if venv:
                add(str(Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")), pid, "running-venv")
        except Exception:
            pass
    return candidates, pids_by_exe


def _walk_venvs(roots: Iterable[str], *, max_depth: int = 5, max_envs: int = 100) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    found = 0
    for root_text in roots:
        root = Path(root_text).expanduser()
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.parts) - base_depth
            if depth >= max_depth:
                dirs[:] = []
            # Avoid very large/irrelevant trees.
            dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", "__pycache__", "site-packages", "dist-packages", "cache", ".cache"}]
            if "pyvenv.cfg" not in files:
                continue
            python_path = current_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            resolved = _candidate_from_path(python_path)
            if resolved:
                result.setdefault(resolved, set()).add(f"venv-scan:{current_path}")
                found += 1
                if found >= max_envs:
                    return result
            dirs[:] = []
    return result


def inspect_python(executable: str, *, sources: Iterable[str] = (), running_pids: Iterable[int] = ()) -> PythonTarget | None:
    probe = r'''
import json, site, sys
try:
    system_sites = list(site.getsitepackages())
except Exception:
    system_sites = []
print(json.dumps({
    "version": sys.version.split()[0],
    "version_info": list(sys.version_info[:3]),
    "prefix": sys.prefix,
    "base_prefix": getattr(sys, "base_prefix", sys.prefix),
    "site_packages": system_sites,
    "user_site": site.getusersitepackages() if hasattr(site, "getusersitepackages") else None,
}))
'''
    try:
        result = _run([executable, "-c", probe], timeout=8)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
        version_info = tuple(int(x) for x in data["version_info"])
        return PythonTarget(
            executable=executable,
            version=str(data["version"]),
            version_info=(version_info + (0, 0, 0))[:3],
            prefix=str(data["prefix"]),
            base_prefix=str(data["base_prefix"]),
            site_packages=tuple(str(p) for p in data.get("site_packages") or []),
            user_site=str(data["user_site"]) if data.get("user_site") else None,
            virtualenv=str(data["prefix"]) != str(data["base_prefix"]),
            source=tuple(sorted(set(sources))),
            running_pids=tuple(sorted(set(int(p) for p in running_pids))),
        )
    except Exception:
        return None


def discover_targets(*, scan_roots: Iterable[str] = DEFAULT_SCAN_ROOTS, max_depth: int = 5, max_envs: int = 100, include_processes: bool = True) -> list[PythonTarget]:
    candidates = _path_candidates()
    pids: dict[str, set[int]] = {}
    if include_processes:
        proc_candidates, proc_pids = _proc_candidates()
        for exe, sources in proc_candidates.items():
            candidates.setdefault(exe, set()).update(sources)
        for exe, values in proc_pids.items():
            pids.setdefault(exe, set()).update(values)
    for exe, sources in _walk_venvs(scan_roots, max_depth=max_depth, max_envs=max_envs).items():
        candidates.setdefault(exe, set()).update(sources)

    # Normalize aliases by environment prefix after inspection; venv python binaries can be symlinks.
    inspected: list[PythonTarget] = []
    seen_key: set[tuple[str, str]] = set()
    for exe, sources in sorted(candidates.items()):
        target = inspect_python(exe, sources=sources, running_pids=pids.get(exe, ()))
        if not target:
            continue
        key = (target.prefix, target.version)
        if key in seen_key:
            continue
        seen_key.add(key)
        inspected.append(target)
    return sorted(inspected, key=lambda t: (not bool(t.running_pids), not t.virtualenv, t.prefix))


def _ensure_pip(target: PythonTarget, *, bootstrap_pip: bool = False) -> tuple[bool, str]:
    check = _run([target.executable, "-m", "pip", "--version"], timeout=10)
    if check.returncode == 0:
        return True, check.stdout.strip()
    if not bootstrap_pip:
        return False, check.stderr.strip() or "pip is unavailable"
    ensure = _run([target.executable, "-m", "ensurepip", "--upgrade"], timeout=60)
    if ensure.returncode != 0:
        return False, ensure.stderr.strip() or "ensurepip failed"
    check = _run([target.executable, "-m", "pip", "--version"], timeout=10)
    return check.returncode == 0, (check.stdout if check.returncode == 0 else check.stderr).strip()


def _target_site(target: PythonTarget) -> Path:
    value = target.primary_site_packages
    if not value:
        raise RuntimeError(f"Could not determine site-packages for {target.executable}")
    return Path(value)


def install_target(target: PythonTarget, *, package: str, bootstrap_pip: bool = False, force: bool = False) -> dict[str, Any]:
    if not target.supported:
        return {"ok": False, "target": target.to_dict(), "error": f"Python {target.version} is unsupported; >=3.10 required"}
    has_pip, pip_detail = _ensure_pip(target, bootstrap_pip=bootstrap_pip)
    if not has_pip:
        return {"ok": False, "target": target.to_dict(), "error": pip_detail}
    cmd = [target.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", package]
    if force:
        cmd.append("--force-reinstall")
    result = _run(cmd, timeout=180)
    if result.returncode != 0:
        return {
            "ok": False,
            "target": target.to_dict(),
            "command": shlex.join(cmd),
            "error": result.stderr.strip() or result.stdout.strip(),
        }
    site_dir = _target_site(target)
    try:
        site_dir.mkdir(parents=True, exist_ok=True)
        pth = site_dir / PTH_FILENAME
        if pth.exists() and pth.read_text(encoding="utf-8") != PTH_CONTENT and not force:
            raise RuntimeError(f"Refusing to overwrite unmanaged {pth}")
        pth.write_text(PTH_CONTENT, encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "target": target.to_dict(), "error": f"SDK installed but startup hook failed: {exc}"}
    verify = _run([target.executable, "-c", "import senda_argus_hooks; print('ok')"], timeout=15)
    return {
        "ok": verify.returncode == 0,
        "target": target.to_dict(),
        "site_packages": str(site_dir),
        "pth": str(site_dir / PTH_FILENAME),
        "pip": pip_detail,
        "restart_required_pids": list(target.running_pids),
        "verify": (verify.stdout if verify.returncode == 0 else verify.stderr).strip(),
    }


def target_status(target: PythonTarget) -> dict[str, Any]:
    site_dir = _target_site(target)
    pth = site_dir / PTH_FILENAME
    managed = False
    if pth.exists():
        try:
            managed = pth.read_text(encoding="utf-8") == PTH_CONTENT
        except Exception:
            pass
    package_check = _run([target.executable, "-c", "import importlib.util; print(bool(importlib.util.find_spec('senda_argus_hooks')))"], timeout=10)
    package_installed = package_check.returncode == 0 and package_check.stdout.strip().endswith("True")
    return {
        "target": target.to_dict(),
        "package_installed": package_installed,
        "hook_installed": pth.exists(),
        "hook_managed": managed,
        "pth": str(pth),
    }


def uninstall_target(target: PythonTarget, *, remove_sdk: bool = False, force: bool = False) -> dict[str, Any]:
    site_dir = _target_site(target)
    pth = site_dir / PTH_FILENAME
    removed_hook = False
    skipped_hook = False
    if pth.exists():
        managed = False
        try:
            managed = pth.read_text(encoding="utf-8") == PTH_CONTENT
        except Exception:
            pass
        if managed or force:
            pth.unlink()
            removed_hook = True
        else:
            skipped_hook = True
    sdk_removed = False
    sdk_error = None
    if remove_sdk:
        result = _run([target.executable, "-m", "pip", "uninstall", "-y", "senda-argus-hooks"], timeout=120)
        sdk_removed = result.returncode == 0
        if result.returncode != 0:
            sdk_error = result.stderr.strip() or result.stdout.strip()
    return {
        "ok": not sdk_error,
        "target": target.to_dict(),
        "removed_hook": removed_hook,
        "skipped_unmanaged_hook": skipped_hook,
        "sdk_removed": sdk_removed,
        "sdk_error": sdk_error,
        "restart_required_pids": list(target.running_pids),
    }


def default_config_path() -> Path:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/etc/senda-argus/hooks.env")
    base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "senda-argus" / "hooks.env"


def write_config(values: dict[str, str | None], *, path: str | None = None, force: bool = False) -> dict[str, Any]:
    config_path = Path(path).expanduser() if path else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            existing[key.strip()] = value.strip()
        if not force:
            # Preserve existing values unless explicitly supplied.
            pass
    merged = dict(existing)
    for key, value in values.items():
        if value is not None:
            merged[key] = str(value)
    merged.setdefault("SENDA_ARGUS_ENABLED", "true")
    merged.setdefault("SENDA_ARGUS_EXPORTERS", "jsonl")
    merged.setdefault("SENDA_ARGUS_PROJECT", "default")
    merged.setdefault("SENDA_ARGUS_ENVIRONMENT", "prod")
    content = "# Managed by Senda-Argus zero-code installer\n" + "\n".join(f"{k}={merged[k]}" for k in sorted(merged)) + "\n"
    config_path.write_text(content, encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except Exception:
        pass
    return {"path": str(config_path), "keys": sorted(merged), "mode": "0600"}
