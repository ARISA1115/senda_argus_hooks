from __future__ import annotations

from threading import RLock
from typing import Any

from senda_argus_hooks.core.identity import (
    data_source_hash,
    derive_purpose_id,
    mcp_data_source_profile,
)

_LOCK = RLock()
_MCP_TOOL_SOURCES: dict[str, dict[str, Any]] = {}


def register_mcp_tool_source(
    *,
    tool_name: str | None,
    mcp_server_name: str | None = None,
    mcp_server_url: str | None = None,
    capability: str | None = None,
    tool_schema_hash: str | None = None,
    tool_description_hash: str | None = None,
) -> None:
    """Register MCP tool data-source metadata for later agent.decision enrichment.

    The Agent application does not need to import identity helpers. MCP clients / 
    instrumentors register the tool source here, and PromptOps instrumentation can
    later enrich agent.decision with selected_tool_purpose_id.
    """
    if not tool_name:
        return
    with _LOCK:
        existing = _MCP_TOOL_SOURCES.get(str(tool_name), {})
        _MCP_TOOL_SOURCES[str(tool_name)] = {
            "tool_name": str(tool_name),
            "mcp_server_name": mcp_server_name if mcp_server_name is not None else existing.get("mcp_server_name"),
            "mcp_server_url": mcp_server_url if mcp_server_url is not None else existing.get("mcp_server_url"),
            "capability": capability if capability is not None else existing.get("capability"),
            "tool_schema_hash": tool_schema_hash if tool_schema_hash is not None else existing.get("tool_schema_hash"),
            "tool_description_hash": tool_description_hash if tool_description_hash is not None else existing.get("tool_description_hash"),
        }


def selected_tool_purpose(
    tool_name: str | None,
    *,
    default_capability: str | None = "security_intelligence",
) -> dict[str, Any] | None:
    """Return purpose metadata for a selected MCP tool.

    If the exact tool was not registered yet, derive a fallback from the tool name
    and default capability. This keeps Agent code simple while still producing a
    useful selected_tool_purpose_id in agent.decision events.
    """
    if not tool_name:
        return None
    with _LOCK:
        source = dict(_MCP_TOOL_SOURCES.get(str(tool_name), {}))

    capability = source.get("capability") or default_capability
    profile = mcp_data_source_profile(
        mcp_server_name=source.get("mcp_server_name"),
        mcp_server_url=source.get("mcp_server_url"),
        tool_name=str(tool_name),
        capability=capability,
        tool_schema_hash=source.get("tool_schema_hash"),
        tool_description_hash=source.get("tool_description_hash"),
    )
    purpose_id = derive_purpose_id(
        mcp_server_name=source.get("mcp_server_name"),
        mcp_server_url=source.get("mcp_server_url"),
        tool_name=str(tool_name),
        capability=capability,
        tool_schema_hash=source.get("tool_schema_hash"),
        tool_description_hash=source.get("tool_description_hash"),
    )
    return {
        "selected_tool_purpose_id": purpose_id,
        "selected_tool_data_source_hash": data_source_hash(profile),
        "selected_tool_purpose_source": "mcp_data_source_hash",
        "selected_tool_purpose_profile": profile,
    }
