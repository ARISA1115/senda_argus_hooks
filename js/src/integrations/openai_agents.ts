import { sha256Value } from "../core/hashing.js";
import { newTraceId, runWithContext } from "../core/context.js";
import { emitEvent } from "../runtime.js";

function spanType(span: any): string {
  return String(span?.spanData?.type ?? span?.data?.type ?? span?.type ?? span?.name ?? "span").toLowerCase();
}
function externalTraceId(value: any): string {
  return String(value?.traceId ?? value?.trace_id ?? value?.id ?? newTraceId());
}
function eventType(type: string, phase: "start" | "end") {
  if (type.includes("function") || type.includes("tool")) return phase === "start" ? "tool_call.requested" : "tool_call.completed";
  if (type.includes("generation") || type.includes("response") || type.includes("model")) return phase === "start" ? "llm.request.started" : "llm.request";
  if (type.includes("handoff")) return phase === "start" ? "agent.handoff.started" : "agent.handoff.completed";
  return phase === "start" ? "agent.step.started" : "agent.step.completed";
}

export class SendaArgusOpenAIAgentsProcessor {
  async onTraceStart(trace: any): Promise<void> {
    const traceId = externalTraceId(trace);
    runWithContext({ traceId }, () => emitEvent("agent.run.started", {
      source: { component: "integration", framework: "openai-agents", sdk: "openai-agents", operation: "trace" },
      data: { agent: { trace_id: traceId, trace_hash: sha256Value(trace?.toJSON?.() ?? trace) } },
      status: "started"
    }));
  }

  async onTraceEnd(trace: any): Promise<void> {
    const traceId = externalTraceId(trace);
    const failed = Boolean(trace?.error);
    runWithContext({ traceId }, () => emitEvent(failed ? "agent.run.failed" : "agent.run.completed", {
      source: { component: "integration", framework: "openai-agents", sdk: "openai-agents", operation: "trace" },
      data: { agent: { trace_id: traceId, trace_hash: sha256Value(trace?.toJSON?.() ?? trace) } },
      status: failed ? "error" : "success",
      error: failed ? { type: trace?.error?.constructor?.name ?? "Error", message: String(trace?.error?.message ?? trace?.error) } : undefined
    }));
  }

  async onSpanStart(span: any): Promise<void> {
    const type = spanType(span);
    const traceId = externalTraceId(span);
    runWithContext({ traceId }, () => emitEvent(eventType(type, "start"), {
      source: { component: "integration", framework: "openai-agents", sdk: "openai-agents", operation: type },
      data: { agent: { span_id: span?.spanId ?? span?.span_id, trace_id: traceId, span_hash: sha256Value(span?.toJSON?.() ?? span) } },
      status: "started"
    }));
  }

  async onSpanEnd(span: any): Promise<void> {
    const type = spanType(span);
    const traceId = externalTraceId(span);
    const failed = Boolean(span?.error);
    let evt = eventType(type, "end");
    if (failed) {
      if (evt === "tool_call.completed") evt = "tool_call.failed";
      else if (evt === "llm.request") evt = "llm.error";
      else evt = "agent.step.failed";
    }
    runWithContext({ traceId }, () => emitEvent(evt, {
      source: { component: "integration", framework: "openai-agents", sdk: "openai-agents", operation: type },
      data: { agent: { span_id: span?.spanId ?? span?.span_id, trace_id: traceId, span_hash: sha256Value(span?.toJSON?.() ?? span) } },
      status: failed ? "error" : "success",
      error: failed ? { type: span?.error?.constructor?.name ?? "Error", message: String(span.error?.message ?? span.error) } : undefined
    }));
  }

  async forceFlush(): Promise<void> {}
  async shutdown(_timeout?: number): Promise<void> {}
}

export function instrumentOpenAIAgents(sdk: any): boolean {
  if (!sdk || typeof sdk.addTraceProcessor !== "function") return false;
  sdk.addTraceProcessor(new SendaArgusOpenAIAgentsProcessor());
  return true;
}
