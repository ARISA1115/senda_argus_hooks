from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def stable_hash(value: Any, *, prefix: str, length: int = 16) -> str:
    """Return a short stable identifier for JSON-like values."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    raw = str(url).strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        netloc = parts.netloc.lower()
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((scheme, netloc, path, "", ""))
    except Exception:
        return raw.lower().rstrip("/")


def derive_agent_id(*, project: str, environment: str, sdk: str | None = None, agent_hint: str | None = None) -> str:
    """Derive an execution-origin identifier.

    This intentionally avoids using MCP server/tool alone. MCP-derived grouping is
    represented by purpose_id / mcp_profile_id so that different agent codebases
    using the same capabilities can be grouped without conflating the executor.
    """
    return stable_hash(
        {
            "project": project,
            "environment": environment,
            "sdk": sdk or "unknown",
            "agent_hint": agent_hint or "default",
        },
        prefix="agent",
    )


def derive_purpose_id(
    *,
    mcp_server_name: str | None = None,
    mcp_server_url: str | None = None,
    tool_name: str | None = None,
    capability: str | None = None,
    tool_schema_hash: str | None = None,
    tool_description_hash: str | None = None,
) -> str:
    """Derive a capability/purpose grouping id from MCP metadata.

    Agents implemented in different repositories will share this id when they use
    the same MCP server/tool/capability profile.
    """
    return stable_hash(
        {
            "mcp_server_name": mcp_server_name or "unknown",
            "mcp_server_url": normalize_url(mcp_server_url),
            "tool_name": tool_name or "unknown",
            "capability": capability or "unknown",
            "tool_schema_hash": tool_schema_hash,
            "tool_description_hash": tool_description_hash,
        },
        prefix="purpose",
    )


def derive_mcp_profile_id(*, mcp_server_name: str | None = None, mcp_server_url: str | None = None, tools: Any = None) -> str:
    return stable_hash(
        {
            "mcp_server_name": mcp_server_name or "unknown",
            "mcp_server_url": normalize_url(mcp_server_url),
            "tools": tools or [],
        },
        prefix="mcp_profile",
    )


def runtime_metadata() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }
