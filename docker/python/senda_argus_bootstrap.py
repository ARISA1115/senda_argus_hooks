from __future__ import annotations

import atexit
import json
import os
import sys
from typing import Any

_BOOTSTRAPPED = False


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


def _exporters() -> list[dict[str, Any]]:
    raw = os.getenv("SENDA_ARGUS_EXPORTERS", os.getenv("SENDA_ARGUS_EXPORTER", "jsonl"))
    names = [item.strip().lower() for item in raw.split(",") if item.strip()]
    result: list[dict[str, Any]] = []
    for name in names:
        if name == "jsonl":
            result.append({
                "type": "jsonl",
                "path": os.getenv("SENDA_ARGUS_JSONL_PATH", "/var/log/senda-argus/events.jsonl"),
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
                "endpoint": os.getenv("SENDA_ARGUS_ENDPOINT", "http://senda-argus:8000"),
                "api_key": os.getenv("SENDA_ARGUS_API_KEY", ""),
                "run_id": _optional("SENDA_ARGUS_RUN_ID"),
                "timeout": _int("SENDA_ARGUS_TIMEOUT", 10),
            })
    return result or [{"type": "jsonl", "path": "/var/log/senda-argus/events.jsonl"}]


def bootstrap() -> dict[str, Any] | None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED or not _bool("SENDA_ARGUS_ENABLED", True):
        return None

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
        atexit.register(shutdown)
        if _bool("SENDA_ARGUS_BOOTSTRAP_DEBUG", False):
            print(f"[senda-argus] bootstrap enabled: {result}", file=sys.stderr)
        return result
    except Exception as exc:
        # Observability must never prevent the Agent from starting.
        if _bool("SENDA_ARGUS_BOOTSTRAP_DEBUG", False):
            print(f"[senda-argus] bootstrap failed open: {exc!r}", file=sys.stderr)
        return None
