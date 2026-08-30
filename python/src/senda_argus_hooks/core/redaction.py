from __future__ import annotations

import copy
import re
from typing import Any

DEFAULT_REDACT_FIELDS = {
    "authorization",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "set-cookie",
    "x-api-key",
}

DEFAULT_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
]


def _redact_str(value: str) -> str:
    redacted = value
    for pattern in DEFAULT_REDACT_PATTERNS:
        if pattern.pattern.startswith("(?i)(bearer"):
            redacted = pattern.sub(r"\1***REDACTED***", redacted)
        else:
            redacted = pattern.sub("***REDACTED***", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in DEFAULT_REDACT_FIELDS:
                result[key] = "***REDACTED***"
            else:
                result[key] = redact_value(item)
        return result
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return _redact_str(value)
    return value


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    cloned = copy.deepcopy(event)
    redacted = redact_value(cloned)
    security = redacted.setdefault("security", {})
    security["redacted"] = True
    return redacted
