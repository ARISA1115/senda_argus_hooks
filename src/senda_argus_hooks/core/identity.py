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


def _clean(value: Any) -> Any:
    """Remove empty values recursively so purpose hashes are stable and compact."""
    if isinstance(value, dict):
        cleaned = {k: _clean(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, "", [], {})}
    if isinstance(value, (list, tuple, set)):
        cleaned = [_clean(v) for v in value]
        return [v for v in cleaned if v not in (None, "", [], {})]
    return value


def data_source_hash(profile: dict[str, Any]) -> str:
    """Return an id for the data source / capability profile used to derive purpose_id."""
    return stable_hash(_clean(profile), prefix="data_source")


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


def mcp_data_source_profile(
    *,
    mcp_server_name: str | None = None,
    mcp_server_url: str | None = None,
    tool_name: str | None = None,
    capability: str | None = None,
    tool_schema_hash: str | None = None,
    tool_description_hash: str | None = None,
) -> dict[str, Any]:
    """Data-source profile for MCP access.

    The generated purpose_id is intentionally based on MCP data source metadata
    such as server URL, server name, tool, capability, and schema/description
    hashes. It does not use raw per-call argument values, so the id groups the
    same data/capability source across runs while arguments remain separately
    auditable via arguments_hash.
    """
    return _clean(
        {
            "source_type": "mcp",
            "mcp_server_name": mcp_server_name or "unknown",
            "mcp_server_url": normalize_url(mcp_server_url),
            "tool_name": tool_name or "unknown",
            "capability": capability or "unknown",
            "tool_schema_hash": tool_schema_hash,
            "tool_description_hash": tool_description_hash,
        }
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
    """Derive a purpose id from MCP data-source/capability metadata."""
    return stable_hash(
        mcp_data_source_profile(
            mcp_server_name=mcp_server_name,
            mcp_server_url=mcp_server_url,
            tool_name=tool_name,
            capability=capability,
            tool_schema_hash=tool_schema_hash,
            tool_description_hash=tool_description_hash,
        ),
        prefix="purpose",
    )


def derive_mcp_profile_id(*, mcp_server_name: str | None = None, mcp_server_url: str | None = None, tools: Any = None) -> str:
    return stable_hash(
        _clean(
            {
                "mcp_server_name": mcp_server_name or "unknown",
                "mcp_server_url": normalize_url(mcp_server_url),
                "tools": tools or [],
            }
        ),
        prefix="mcp_profile",
    )


def derive_tool_purpose_id(
    *,
    framework: str | None = None,
    tool_name: str | None = None,
    tool_type: str | None = None,
    operation: str | None = None,
    target: str | None = None,
    tool_schema_hash: str | None = None,
    tool_description_hash: str | None = None,
) -> str:
    """Derive a stable purpose id for non-MCP framework tools."""
    return stable_hash(
        _clean(
            {
                "source_type": "framework_tool",
                "framework": framework or "unknown",
                "tool_name": tool_name or "unknown",
                "tool_type": tool_type or "unknown",
                "operation": operation or "unknown",
                "target": normalize_url(target) if target else None,
                "tool_schema_hash": tool_schema_hash,
                "tool_description_hash": tool_description_hash,
            }
        ),
        prefix="purpose",
    )


def rag_data_source_profile(
    *,
    framework: str | None = None,
    component_name: str | None = None,
    component_type: str | None = None,
    index_name: str | None = None,
    collection_name: str | None = None,
    vector_store: str | None = None,
    data_source: str | None = None,
    data_source_url: str | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Data-source profile for RAG access.

    The generated purpose id groups access to the same RAG data source/index.
    Query text is intentionally excluded; query_hash is logged separately.
    """
    return _clean(
        {
            "source_type": "rag",
            "framework": framework or "unknown",
            "component_name": component_name or "unknown",
            "component_type": component_type or "unknown",
            "index_name": index_name,
            "collection_name": collection_name,
            "vector_store": vector_store,
            "data_source": data_source,
            "data_source_url": normalize_url(data_source_url),
            "target": normalize_url(target) if target else None,
        }
    )


def derive_retrieval_purpose_id(
    *,
    framework: str | None = None,
    retriever_name: str | None = None,
    retriever_type: str | None = None,
    index_name: str | None = None,
    collection_name: str | None = None,
    vector_store: str | None = None,
    data_source: str | None = None,
    data_source_url: str | None = None,
    target: str | None = None,
) -> str:
    """Derive a stable purpose id for RAG retrieval/index access."""
    return stable_hash(
        rag_data_source_profile(
            framework=framework,
            component_name=retriever_name,
            component_type=retriever_type or "retriever",
            index_name=index_name,
            collection_name=collection_name,
            vector_store=vector_store,
            data_source=data_source,
            data_source_url=data_source_url,
            target=target,
        ),
        prefix="purpose",
    )


def derive_rag_query_purpose_id(
    *,
    framework: str | None = None,
    query_engine_name: str | None = None,
    query_engine_type: str | None = None,
    index_name: str | None = None,
    collection_name: str | None = None,
    vector_store: str | None = None,
    data_source: str | None = None,
    data_source_url: str | None = None,
    target: str | None = None,
) -> str:
    """Derive a stable purpose id for RAG query-engine access."""
    return stable_hash(
        rag_data_source_profile(
            framework=framework,
            component_name=query_engine_name,
            component_type=query_engine_type or "query_engine",
            index_name=index_name,
            collection_name=collection_name,
            vector_store=vector_store,
            data_source=data_source,
            data_source_url=data_source_url,
            target=target,
        ),
        prefix="purpose",
    )


def derive_embedding_purpose_id(
    *,
    framework: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    embedding_type: str | None = None,
    data_source: str | None = None,
    data_source_url: str | None = None,
) -> str:
    """Derive a stable purpose id for embedding generation/use."""
    return stable_hash(
        _clean(
            {
                "source_type": "embedding",
                "framework": framework or "unknown",
                "provider": provider or "unknown",
                "model": model or "unknown",
                "embedding_type": embedding_type or "text",
                "data_source": data_source,
                "data_source_url": normalize_url(data_source_url),
            }
        ),
        prefix="purpose",
    )


def runtime_metadata() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }
