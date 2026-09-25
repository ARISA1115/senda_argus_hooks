from __future__ import annotations

import atexit
import json
import os
import sys
from pathlib import Path
from typing import Any

_BOOTSTRAPPED = False
_BOOTSTRAP_RESULT: dict[str, Any] | None = None


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else None


def _actor() -> dict[str, Any]:
    raw = os.getenv("SENDA_ARGUS_ACTOR_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _default_jsonl_path() -> str:
    # Keep the default writable for ordinary users and service accounts.
    state_home = os.getenv("XDG_STATE_HOME")
    if state_home:
        return str(Path(state_home) / "senda-argus" / "events.jsonl")
    try:
        return str(Path.home() / ".local" / "state" / "senda-argus" / "events.jsonl")
    except Exception:
        return "./senda-events.jsonl"


def _exporters() -> list[dict[str, Any]]:
    raw = os.getenv("SENDA_ARGUS_EXPORTERS", os.getenv("SENDA_ARGUS_EXPORTER", "jsonl"))
    names = [item.strip().lower() for item in raw.split(",") if item.strip()]
    result: list[dict[str, Any]] = []
    for name in names:
        if name == "jsonl":
            result.append({
                "type": "jsonl",
                "path": os.getenv("SENDA_ARGUS_JSONL_PATH", _default_jsonl_path()),
            })
        elif name == "stdout":
            result.append({
                "type": "stdout",
                "pretty": _bool("SENDA_ARGUS_STDOUT_PRETTY", False),
            })
        elif name == "null":
            result.append({"type": "null"})
        elif name == "argus":
            result.append({
                "type": "argus",
                "endpoint": os.getenv("SENDA_ARGUS_ENDPOINT", "http://localhost:8000"),
                "api_key": os.getenv("SENDA_ARGUS_API_KEY", ""),
                "run_id": _optional("SENDA_ARGUS_RUN_ID"),
                "timeout": _int("SENDA_ARGUS_TIMEOUT", 10),
            })
    return result or [{"type": "jsonl", "path": _default_jsonl_path()}]


def bootstrap() -> dict[str, Any] | None:
    """Enable Senda-Argus instrumentation during Python interpreter startup.

    This function is intended to be called from a .pth file. It is deliberately
    fail-open: errors in observability never block the target Agent process.
    """
    global _BOOTSTRAPPED, _BOOTSTRAP_RESULT
    try:
        from senda_argus_hooks.config import load_config_files
        load_config_files()
    except Exception:
        pass
    if _BOOTSTRAPPED or not _bool("SENDA_ARGUS_ENABLED", True):
        return _BOOTSTRAP_RESULT

    try:
        from senda_argus_hooks import register, shutdown

        result = register(
            project=os.getenv("SENDA_ARGUS_PROJECT", "default"),
            environment=os.getenv("SENDA_ARGUS_ENVIRONMENT", "prod"),
            exporters=_exporters(),
            auto_instrument=True,
            instrument_openai=_bool("SENDA_ARGUS_INSTRUMENT_OPENAI", True),
            instrument_anthropic=_bool("SENDA_ARGUS_INSTRUMENT_ANTHROPIC", True),
            instrument_litellm=_bool("SENDA_ARGUS_INSTRUMENT_LITELLM", True),
            instrument_ollama=_bool("SENDA_ARGUS_INSTRUMENT_OLLAMA", True),
            instrument_mcp=_bool("SENDA_ARGUS_INSTRUMENT_MCP", True),
            instrument_argus_sdk=_bool("SENDA_ARGUS_INSTRUMENT_ARGUS_SDK", True),
            instrument_openai_agents=_bool("SENDA_ARGUS_INSTRUMENT_OPENAI_AGENTS", True),
            capture_prompt=_bool("SENDA_ARGUS_CAPTURE_PROMPT", False),
            capture_response=_bool("SENDA_ARGUS_CAPTURE_RESPONSE", False),
            capture_arguments=_bool("SENDA_ARGUS_CAPTURE_ARGUMENTS", False),
            capture_result=_bool("SENDA_ARGUS_CAPTURE_RESULT", False),
            capture_hash=_bool("SENDA_ARGUS_CAPTURE_HASH", True),
            redact=_bool("SENDA_ARGUS_REDACT", True),
            actor=_actor(),
            tenant_id=_optional("SENDA_ARGUS_TENANT_ID"),
            session_id=_optional("SENDA_ARGUS_SESSION_ID"),
            conversation_id=_optional("SENDA_ARGUS_CONVERSATION_ID"),
            run_id=_optional("SENDA_ARGUS_RUN_ID"),
            turn_id=_optional("SENDA_ARGUS_TURN_ID"),
            agent_id=_optional("SENDA_ARGUS_AGENT_ID"),
            purpose_id=_optional("SENDA_ARGUS_PURPOSE_ID"),
            agent_hint=_optional("SENDA_ARGUS_AGENT_HINT"),
            batch_size=_int("SENDA_ARGUS_BATCH_SIZE", 1),
        )
        _BOOTSTRAPPED = True
        _BOOTSTRAP_RESULT = result
        atexit.register(shutdown)
        if _bool("SENDA_ARGUS_BOOTSTRAP_DEBUG", False):
            print(f"[senda-argus] auto-hook enabled: {result}", file=sys.stderr)
        return result
    except Exception as exc:
        # Observability must never prevent the Agent from starting.
        if _bool("SENDA_ARGUS_BOOTSTRAP_DEBUG", False):
            print(f"[senda-argus] auto-hook bootstrap failed: {exc!r}", file=sys.stderr)
        return None


def is_bootstrapped() -> bool:
    return _BOOTSTRAPPED
