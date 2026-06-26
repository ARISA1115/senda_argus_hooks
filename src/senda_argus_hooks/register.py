from __future__ import annotations

from typing import Any

# Import exporters package to register built-in exporters.
import senda_argus_hooks.exporters  # noqa: F401
from senda_argus_hooks.core.context import RuntimeConfig
from senda_argus_hooks.core.queue import EventBus
from senda_argus_hooks.core.runtime import configure, get_bus
from senda_argus_hooks.exporters.registry import create_exporter
from senda_argus_hooks.instrumentors import AnthropicInstrumentor, ArgusSDKInstrumentor, LiteLLMInstrumentor, MCPPythonInstrumentor, OpenAIInstrumentor

_ACTIVE_INSTRUMENTORS: list[Any] = []


def register(
    *,
    project: str = "default",
    environment: str = "dev",
    exporters: list[dict[str, Any]] | None = None,
    auto_instrument: bool = False,
    instrument_openai: bool = True,
    instrument_anthropic: bool = True,
    instrument_litellm: bool = True,
    instrument_mcp: bool = True,
    instrument_argus_sdk: bool = True,
    capture_prompt: bool = True,
    capture_response: bool = True,
    capture_arguments: bool = True,
    capture_result: bool = True,
    capture_hash: bool = True,
    redact: bool = True,
    actor: dict[str, Any] | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
    agent_id: str | None = None,
    purpose_id: str | None = None,
    agent_hint: str | None = None,
    batch_size: int = 1,
) -> dict[str, Any]:
    """Configure Senda-Argus Hooks.

    By default this uses JSONL file exporter at ./senda-events.jsonl.
    Missing optional SDKs are ignored, so users can enable auto_instrument safely.
    """
    exporter_configs = exporters or [{"type": "jsonl", "path": "./senda-events.jsonl"}]
    exporter_instances = [create_exporter(cfg) for cfg in exporter_configs]
    cfg = RuntimeConfig(
        project=project,
        environment=environment,
        capture_prompt=capture_prompt,
        capture_response=capture_response,
        capture_arguments=capture_arguments,
        capture_result=capture_result,
        capture_hash=capture_hash,
        redact=redact,
        actor=actor or {},
        tenant_id=tenant_id,
        session_id=session_id,
        conversation_id=conversation_id,
        run_id=run_id,
        turn_id=turn_id,
        agent_id=agent_id,
        purpose_id=purpose_id,
        agent_hint=agent_hint,
    )
    bus = EventBus(exporters=exporter_instances, batch_size=batch_size)
    configure(cfg, bus)

    installed = {}
    if auto_instrument:
        if instrument_openai:
            installed["openai"] = _activate(OpenAIInstrumentor())
        if instrument_anthropic:
            installed["anthropic"] = _activate(AnthropicInstrumentor())
        if instrument_litellm:
            installed["litellm"] = _activate(LiteLLMInstrumentor())
        if instrument_mcp:
            installed["mcp_python"] = _activate(MCPPythonInstrumentor())
        if instrument_argus_sdk:
            installed["argus_sdk"] = _activate(ArgusSDKInstrumentor())
    return {"project": project, "environment": environment, "instrumentors": installed}


def _activate(instrumentor) -> bool:
    try:
        result = instrumentor.instrument()
        if result:
            _ACTIVE_INSTRUMENTORS.append(instrumentor)
        return bool(result)
    except Exception:
        return False


def flush() -> None:
    get_bus().flush()


def shutdown() -> None:
    flush()
    for instrumentor in list(_ACTIVE_INSTRUMENTORS):
        try:
            instrumentor.uninstrument()
        except Exception:
            pass
    _ACTIVE_INSTRUMENTORS.clear()
    get_bus().shutdown()


def unregister() -> None:
    shutdown()
