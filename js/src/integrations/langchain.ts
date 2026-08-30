import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { emitEvent, getConfig } from "../runtime.js";
import { safeValue } from "../instrumentors/common.js";

const starts = new Map<string, number>();
const latency = (runId?: string) => {
  const k = runId ?? "";
  const started = starts.get(k);
  if (k) starts.delete(k);
  return started == null ? undefined : Math.round(performance.now() - started);
};

function maybeBody(value: unknown, capture: boolean) {
  return capture ? safeValue(value) : { hash: sha256Value(safeValue(value)) };
}

export class SendaArgusLangChainCallbackHandler {
  name = "senda_argus";

  handleLLMStart(serialized: any, prompts: any, runId?: string) {
    if (runId) starts.set(runId, performance.now());
    emitEvent("llm.request.started", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "llm" },
      data: { llm: { framework: "langchain", model: serialized?.name ?? serialized?.id?.at?.(-1), input: maybeBody(prompts, getConfig().capturePrompt) } },
      status: "started"
    });
  }

  handleChatModelStart(serialized: any, messages: any, runId?: string) {
    return this.handleLLMStart(serialized, messages, runId);
  }

  handleLLMEnd(output: any, runId?: string) {
    emitEvent("llm.request", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "llm" },
      data: { llm: { framework: "langchain", output: maybeBody(output, getConfig().captureResponse) } },
      status: "success", latencyMs: latency(runId)
    });
  }

  handleLLMError(error: any, runId?: string) {
    emitEvent("llm.error", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "llm" },
      status: "error", latencyMs: latency(runId),
      error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) }
    });
  }

  handleToolStart(tool: any, input: any, runId?: string) {
    if (runId) starts.set(runId, performance.now());
    emitEvent("tool_call.requested", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "tool" },
      data: { tool: { framework: "langchain", name: tool?.name ?? tool?.id?.at?.(-1), arguments: maybeBody(input, getConfig().captureArguments) } },
      status: "requested"
    });
  }

  handleToolEnd(output: any, runId?: string) {
    emitEvent("tool_call.completed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "tool" },
      data: { tool: { framework: "langchain", result: maybeBody(output, getConfig().captureResult) } },
      status: "success", latencyMs: latency(runId)
    });
  }

  handleToolError(error: any, runId?: string) {
    emitEvent("tool_call.failed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "tool" },
      status: "error", latencyMs: latency(runId),
      error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) }
    });
  }

  handleChainStart(serialized: any, input: any, runId?: string) {
    if (runId) starts.set(runId, performance.now());
    emitEvent("agent.step.started", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "chain" },
      data: { agent: { framework: "langchain", name: serialized?.name ?? serialized?.id?.at?.(-1), input_hash: sha256Value(input) } },
      status: "started"
    });
  }

  handleChainEnd(output: any, runId?: string) {
    emitEvent("agent.step.completed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "chain" },
      data: { agent: { framework: "langchain", output_hash: sha256Value(output) } },
      status: "success", latencyMs: latency(runId)
    });
  }

  handleChainError(error: any, runId?: string) {
    emitEvent("agent.step.failed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "chain" },
      status: "error", latencyMs: latency(runId),
      error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) }
    });
  }

  handleAgentAction(action: any) {
    emitEvent("agent.decision", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "agent_action" },
      data: { agent: { framework: "langchain", selected_tool: action?.tool, action_hash: sha256Value(action) } },
      status: "success"
    });
  }
}

export function langChainCallbackHandler() {
  return new SendaArgusLangChainCallbackHandler();
}
