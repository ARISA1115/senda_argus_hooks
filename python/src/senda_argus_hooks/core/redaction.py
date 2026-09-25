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

# 値ベースの秘匿パターン。固定キー集合に一致しない引数の値として資格情報が渡っても平文で
# 通過させないよう、主要プロバイダのトークン形式を一箇所へ集約して持つ。bearer だけは接頭辞を
# 残して以降を伏せ、他は一致部分を丸ごと伏せる。
DEFAULT_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),          # OpenAI / Anthropic 系 (sk-, sk-ant-)
    re.compile(r"AKIA[0-9A-Z]{16}"),                 # AWS access key id
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),  # HTTP Authorization: Bearer
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"),        # GitHub PAT / OAuth / server / refresh
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),     # GitHub fine-grained PAT
    # Slack: bot(xoxb)/user(xoxp)/legacy(xoxa,xoxr,xoxs)/rotating refresh(xoxe)/app-level(xapp)。
    # xox[baprs]- だけだと現行の app-level(xapp-)と refresh(xoxe-)を取りこぼす。
    re.compile(r"(?:xox[baprse]|xapp)-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),           # Google API key
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),        # Google OAuth access token
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
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
