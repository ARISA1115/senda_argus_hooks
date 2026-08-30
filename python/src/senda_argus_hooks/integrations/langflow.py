from __future__ import annotations

from pathlib import Path
from typing import Any

from senda_argus_hooks.integrations.langchain import SendaArgusCallbackHandler


def register_langflow_hooks(
    *,
    project: str = "langflow-agent",
    environment: str = "dev",
    export_path: str | Path = "./langflow-events.jsonl",
    auto_instrument: bool = True,
    capture_prompt: bool = False,
    capture_response: bool = False,
    capture_arguments: bool = True,
    capture_result: bool = False,
    redact: bool = True,
    actor: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Register Senda-Argus Hooks for Langflow-based workflows.

    Langflow can execute LangChain, LangGraph, RAG, MCP, and custom Python
    components. This helper keeps Langflow support intentionally lightweight:
    it configures the normal Senda-Argus runtime and enables the existing SDK
    and MCP hooks where possible. For LangChain-compatible Langflow components,
    use :func:`langflow_callback_handler` as a callback handler.

    Prompt and response capture are disabled by default. Argument capture is
    enabled by default so tool and MCP intent can be audited without storing
    full result bodies.
    """
    from senda_argus_hooks.register import register

    exporters = kwargs.pop("exporters", None) or [{"type": "jsonl", "path": str(export_path)}]
    return register(
        project=project,
        environment=environment,
        exporters=exporters,
        auto_instrument=auto_instrument,
        capture_prompt=capture_prompt,
        capture_response=capture_response,
        capture_arguments=capture_arguments,
        capture_result=capture_result,
        redact=redact,
        actor=actor,
        **kwargs,
    )


def langflow_callback_handler(*, capture_payloads: bool | None = None) -> SendaArgusCallbackHandler:
    """Return a LangChain-compatible callback handler labelled as Langflow.

    Use this for Langflow components that expose LangChain callbacks. It emits
    the same normalized events as the LangChain integration, but marks the
    framework/source as ``langflow`` for easier filtering.
    """
    return SendaArgusCallbackHandler(framework="langflow", capture_payloads=capture_payloads)
