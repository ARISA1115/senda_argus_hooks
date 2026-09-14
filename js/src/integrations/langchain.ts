import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { newTraceId, runWithContext } from "../core/context.js";
import { emitEvent, getConfig } from "../runtime.js";
import { safeValue } from "../instrumentors/common.js";

const starts = new Map<string, number>();
const traces = new Map<string, string>();

function key(runId?: unknown): string {
  return runId == null ? "" : String(runId);
}
function begin(runId?: unknown): string {
  const k = key(runId);
  const traceId = newTraceId();
  if (k) {
    starts.set(k, performance.now());
    traces.set(k, traceId);
  }
  return traceId;
}
function trace(runId?: unknown): string {
  const k = key(runId);
  return (k && traces.get(k)) || newTraceId();
}
function finish(runId?: unknown): { traceId: string; latencyMs?: number } {
  const k = key(runId);
  const traceId = (k && traces.get(k)) || newTraceId();
  const started = k ? starts.get(k) : undefined;
  if (k) {
    traces.delete(k);
    starts.delete(k);
  }
  return { traceId, latencyMs: started == null ? undefined : Math.round(performance.now() - started) };
}

function maybeBody(value: unknown, capture: boolean) {
  return capture ? safeValue(value) : { hash: sha256Value(safeValue(value)) };
}

export class SendaArgusLangChainCallbackHandler {
  name = "senda_argus";

  handleLLMStart(serialized: any, prompts: any, runId?: unknown) {
    const traceId = begin(runId);
    return runWithContext({ traceId, runId: key(runId) || undefined }, () => emitEvent("llm.request.started", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "llm" },
      data: { llm: { framework: "langchain", model: serialized?.name ?? serialized?.id?.at?.(-1), input: maybeBody(prompts, getConfig().capturePrompt) } },
      status: "started"
    }));
  }

  handleChatModelStart(serialized: any, messages: any, runId?: unknown) {
    return this.handleLLMStart(serialized, messages, runId);
  }

  handleLLMEnd(output: any, runId?: unknown) {
    const lifecycle = finish(runId);
    return runWithContext({ traceId: lifecycle.traceId, runId: key(runId) || undefined }, () => emitEvent("llm.request", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "llm" },
      data: { llm: { framework: "langchain", output: maybeBody(output, getConfig().captureResponse) } },
      status: "success", latencyMs: lifecycle.latencyMs
    }));
  }

  handleLLMError(error: any, runId?: unknown) {
    const lifecycle = finish(runId);
    return runWithContext({ traceId: lifecycle.traceId, runId: key(runId) || undefined }, () => emitEvent("llm.error", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "llm" },
      status: "error", latencyMs: lifecycle.latencyMs,
      error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) }
    }));
  }

  handleToolStart(tool: any, input: any, runId?: unknown) {
    const traceId = begin(runId);
    return runWithContext({ traceId, runId: key(runId) || undefined }, () => emitEvent("tool_call.requested", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "tool" },
      data: { tool: { framework: "langchain", name: tool?.name ?? tool?.id?.at?.(-1), arguments: maybeBody(input, getConfig().captureArguments) } },
      status: "requested"
    }));
  }

  handleToolEnd(output: any, runId?: unknown) {
    const lifecycle = finish(runId);
    return runWithContext({ traceId: lifecycle.traceId, runId: key(runId) || undefined }, () => emitEvent("tool_call.completed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "tool" },
      data: { tool: { framework: "langchain", result: maybeBody(output, getConfig().captureResult) } },
      status: "success", latencyMs: lifecycle.latencyMs
    }));
  }

  handleToolError(error: any, runId?: unknown) {
    const lifecycle = finish(runId);
    return runWithContext({ traceId: lifecycle.traceId, runId: key(runId) || undefined }, () => emitEvent("tool_call.failed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "tool" },
      status: "error", latencyMs: lifecycle.latencyMs,
      error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) }
    }));
  }

  handleChainStart(serialized: any, input: any, runId?: unknown) {
    const traceId = begin(runId);
    return runWithContext({ traceId, runId: key(runId) || undefined }, () => emitEvent("agent.step.started", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "chain" },
      data: { agent: { framework: "langchain", name: serialized?.name ?? serialized?.id?.at?.(-1), input_hash: sha256Value(input) } },
      status: "started"
    }));
  }

  handleChainEnd(output: any, runId?: unknown) {
    const lifecycle = finish(runId);
    return runWithContext({ traceId: lifecycle.traceId, runId: key(runId) || undefined }, () => emitEvent("agent.step.completed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "chain" },
      data: { agent: { framework: "langchain", output_hash: sha256Value(output) } },
      status: "success", latencyMs: lifecycle.latencyMs
    }));
  }

  handleChainError(error: any, runId?: unknown) {
    const lifecycle = finish(runId);
    return runWithContext({ traceId: lifecycle.traceId, runId: key(runId) || undefined }, () => emitEvent("agent.step.failed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "chain" },
      status: "error", latencyMs: lifecycle.latencyMs,
      error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) }
    }));
  }

  handleAgentAction(action: any, runId?: unknown) {
    const traceId = trace(runId);
    return runWithContext({ traceId, runId: key(runId) || undefined }, () => emitEvent("agent.decision", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "agent_action" },
      data: { agent: { framework: "langchain", selected_tool: action?.tool, action_hash: sha256Value(action) } },
      status: "success"
    }));
  }
}

export function langChainCallbackHandler() {
  return new SendaArgusLangChainCallbackHandler();
}
