#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "python" / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from senda_argus_hooks.zerocode import (  # noqa: E402
    DEFAULT_SCAN_ROOTS,
    discover_targets,
    install_target,
    target_status,
    uninstall_target,
    write_config,
    inspect_python,
)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def targets(args):
    roots = args.scan_root or list(DEFAULT_SCAN_ROOTS)
    found = discover_targets(scan_roots=roots, max_depth=args.max_depth, max_envs=args.max_envs, include_processes=not args.no_processes)
    if args.python:
        wanted = {str(Path(x).expanduser().absolute()) for x in args.python}
        by_exe = {str(Path(t.executable).absolute()): t for t in found}
        for item in sorted(wanted):
            if item not in by_exe:
                target = inspect_python(item, sources=["explicit"])
                if target is not None:
                    by_exe[item] = target
        found = [by_exe[item] for item in sorted(wanted) if item in by_exe]
    return found


def add_scan_options(p):
    p.add_argument("--scan-root", action="append", help="Directory to scan for pyvenv.cfg; repeatable")
    p.add_argument("--max-depth", type=int, default=5)
    p.add_argument("--max-envs", type=int, default=100)
    p.add_argument("--no-processes", action="store_true", help="Do not inspect /proc for running Python Agents")
    p.add_argument("--python", action="append", help="Limit to an exact Python executable; repeatable")


def build_parser():
    parser = argparse.ArgumentParser(description="Senda-Argus zero-code installer for existing Python Agent environments")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Discover Python/venv environments and running Python Agents")
    add_scan_options(scan)

    status = sub.add_parser("status", help="Show SDK/startup-hook status for discovered environments")
    add_scan_options(status)

    install = sub.add_parser("install", help="Install SDK and startup hook into discovered environments")
    add_scan_options(install)
    install.add_argument("--package", help="Wheel or python/ source directory. Defaults to local python/ tree.")
    install.add_argument("--all", action="store_true", help="Install to all supported discovered environments")
    install.add_argument("--yes", action="store_true", help="Required with --all to avoid accidental fleet-wide changes")
    install.add_argument("--bootstrap-pip", action="store_true", help="Use ensurepip when a target environment has no pip")
    install.add_argument("--force", action="store_true", help="Force reinstall SDK / managed hook")
    install.add_argument("--config", help="Config file path; root default: /etc/senda-argus/hooks.env")
    install.add_argument("--endpoint")
    install.add_argument("--api-key", help="API key (prefer --api-key-file to avoid shell history/process args)")
    install.add_argument("--api-key-file", help="Read API key from a file")
    install.add_argument("--project")
    install.add_argument("--environment", default=None)
    install.add_argument("--exporters", help="argus,jsonl,stdout")
    install.add_argument("--jsonl-path")
    install.add_argument("--capture-prompt", choices=["true", "false"])
    install.add_argument("--capture-response", choices=["true", "false"])
    install.add_argument("--capture-arguments", choices=["true", "false"])
    install.add_argument("--capture-result", choices=["true", "false"])
    install.add_argument("--no-config", action="store_true")

    uninstall = sub.add_parser("uninstall", help="Remove startup hooks from discovered environments")
    add_scan_options(uninstall)
    uninstall.add_argument("--all", action="store_true")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--remove-sdk", action="store_true")
    uninstall.add_argument("--force", action="store_true")
    return parser


def select(found, args):
    supported = [t for t in found if t.supported]
    if args.all:
        if not args.yes:
            raise SystemExit("--all requires --yes")
        return supported
    if args.python:
        return supported
    # Conservative default: current interpreter only. Explicit --all is needed for fleet changes.
    current_prefix = str(Path(sys.prefix).resolve())
    current = [t for t in supported if str(Path(t.prefix).resolve()) == current_prefix]
    return current[:1]


def main(argv=None):
    args = build_parser().parse_args(argv)
    found = targets(args)
    if args.command == "scan":
        emit({"count": len(found), "targets": [t.to_dict() for t in found]})
        return 0
    if args.command == "status":
        emit({"count": len(found), "targets": [target_status(t) for t in found if t.supported]})
        return 0
    selected = select(found, args)
    if not selected:
        emit({"ok": False, "error": "No supported Python target selected", "discovered": [t.to_dict() for t in found]})
        return 2
    if args.command == "install":
        package = args.package
        if not package:
            wheel_dir = ROOT / "dist"
            wheels = sorted(wheel_dir.glob("senda_argus_hooks-*.whl")) if wheel_dir.is_dir() else []
            package = str(wheels[-1]) if wheels else str(ROOT / "python")
        package = str(Path(package).expanduser().resolve())
        config = None
        if not args.no_config:
            api_key = args.api_key
            if args.api_key_file:
                api_key = Path(args.api_key_file).expanduser().read_text(encoding="utf-8").strip()
            values = {
                "SENDA_ARGUS_ENABLED": "true",
                "SENDA_ARGUS_ENDPOINT": args.endpoint,
                "SENDA_ARGUS_API_KEY": api_key,
                "SENDA_ARGUS_PROJECT": args.project,
                "SENDA_ARGUS_ENVIRONMENT": args.environment,
                "SENDA_ARGUS_EXPORTERS": args.exporters,
                "SENDA_ARGUS_JSONL_PATH": args.jsonl_path,
                "SENDA_ARGUS_CAPTURE_PROMPT": args.capture_prompt,
                "SENDA_ARGUS_CAPTURE_RESPONSE": args.capture_response,
                "SENDA_ARGUS_CAPTURE_ARGUMENTS": args.capture_arguments,
                "SENDA_ARGUS_CAPTURE_RESULT": args.capture_result,
            }
            config = write_config(values, path=args.config, force=args.force)
        results = [install_target(t, package=package, bootstrap_pip=args.bootstrap_pip, force=args.force) for t in selected]
        emit({"ok": all(x.get("ok") for x in results), "package": package, "config": config, "results": results, "note": "Running Agent processes listed in restart_required_pids must be restarted before the hook becomes active."})
        return 0 if all(x.get("ok") for x in results) else 1
    if args.command == "uninstall":
        results = [uninstall_target(t, remove_sdk=args.remove_sdk, force=args.force) for t in selected]
        emit({"ok": all(x.get("ok") for x in results), "results": results})
        return 0 if all(x.get("ok") for x in results) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
