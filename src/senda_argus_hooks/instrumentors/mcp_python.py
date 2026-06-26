from __future__ import annotations

import time
from typing import Any, Callable

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.identity import derive_mcp_profile_id, derive_purpose_id, normalize_url
from senda_argus_hooks.core.runtime import emit_event, get_config
from .base import BaseInstrumentor


class MCPPythonInstrumentor(BaseInstrumentor):
    name = "mcp_python"

    def __init__(self):
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        candidates = []
        try:
            from mcp import ClientSession
            candidates.append((ClientSession, "call_tool", "call_tool"))
            candidates.append((ClientSession, "read_resource", "read_resource"))
            candidates.append((ClientSession, "list_tools", "list_tools"))
            candidates.append((ClientSession, "list_resources", "list_resources"))
        except Exception:
            pass
        patched = False
        for cls, method_name, op in candidates:
            original = getattr(cls, method_name, None)
            if original is None or hasattr(original, "__senda_patched__"):
                continue
            wrapped = self._wrap(original, op)
            setattr(wrapped, "__senda_patched__", True)
            setattr(cls, method_name, wrapped)
            self._patches.append((cls, method_name, original))
            patched = True
        return patched

    def _wrap(self, original: Callable, operation: str) -> Callable:
        def sync_wrapper(obj, *args, **kwargs):
            result = original(obj, *args, **kwargs)
            if hasattr(result, "__await__"):
                async def awaited():
                    return await self._observe_async_call(original_result=result, operation=operation, obj=obj, args=args, kwargs=kwargs)
                return awaited()
            return result
        return sync_wrapper

    async def _observe_async_call(self, *, original_result, operation: str, obj, args, kwargs):
        cfg = get_config()
        started = time.perf_counter()
        meta = _mcp_metadata(obj, operation, args, kwargs)
        purpose_id = meta["purpose_id"]
        if operation == "call_tool":
            emit_event(
                "mcp.tool_call.requested",
                source={"component": "instrumentor", "sdk": "mcp_python", "operation": operation},
                data={"mcp": meta},
                status="start",
                purpose_id=purpose_id,
            )
        try:
            response = await original_result
            latency_ms = int((time.perf_counter() - started) * 1000)
            result_payload = _safe_response(response)
            data = {"mcp": {**meta}}
            if cfg.capture_result:
                data["mcp"]["result"] = result_payload
            data["mcp"]["result_hash"] = sha256_value(result_payload)
            emit_event(
                "mcp.tool_call.completed" if operation == "call_tool" else f"mcp.{operation}.completed",
                source={"component": "instrumentor", "sdk": "mcp_python", "operation": operation},
                data=data,
                status="success",
                latency_ms=latency_ms,
                purpose_id=purpose_id,
            )
            return response
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            emit_event(
                "mcp.tool_call.failed" if operation == "call_tool" else f"mcp.{operation}.failed",
                source={"component": "instrumentor", "sdk": "mcp_python", "operation": operation},
                data={"mcp": meta},
                status="error",
                latency_ms=latency_ms,
                error={"type": exc.__class__.__name__, "message": str(exc)},
                purpose_id=purpose_id,
            )
            raise

    def uninstrument(self) -> bool:
        for cls, method_name, original in self._patches:
            setattr(cls, method_name, original)
        self._patches = []
        return True


def _extract_arguments(operation: str, args, kwargs) -> dict[str, Any]:
    if operation == "call_tool":
        return {"tool": args[0] if args else kwargs.get("name"), "arguments": args[1] if len(args) > 1 else kwargs.get("arguments")}
    return {"args": args, "kwargs": kwargs}


def _mcp_metadata(obj, operation: str, args, kwargs) -> dict[str, Any]:
    cfg = get_config()
    arguments = _extract_arguments(operation, args, kwargs)
    tool_name = arguments.get("tool")
    server_name = getattr(obj, "server", None) or getattr(obj, "server_name", None) or getattr(obj, "name", None) or "unknown"
    server_url = getattr(obj, "url", None) or getattr(obj, "base_url", None) or getattr(obj, "server_url", None)
    capability = kwargs.get("capability") or getattr(obj, "capability", None)
    args_hash = sha256_value(arguments)
    mcp_profile_id = derive_mcp_profile_id(mcp_server_name=server_name, mcp_server_url=server_url)
    purpose_id = derive_purpose_id(mcp_server_name=server_name, mcp_server_url=server_url, tool_name=str(tool_name), capability=capability)
    meta = {
        "operation": operation,
        "server": server_name,
        "server_url": normalize_url(str(server_url)) if server_url else None,
        "tool": tool_name,
        "capability": capability,
        "mcp_profile_id": mcp_profile_id,
        "purpose_id": purpose_id,
        "arguments_hash": args_hash,
    }
    if cfg.capture_arguments:
        meta["arguments"] = arguments
    return meta


def _safe_response(response: Any) -> Any:
    for attr in ("model_dump", "dict"):
        if hasattr(response, attr):
            try:
                return getattr(response, attr)()
            except Exception:
                pass
    return str(response)
