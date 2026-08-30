import { sha256Value } from "../core/hashing.js";
import { emitEvent } from "../runtime.js";

function spanType(span: any): string {
  return String(span?.spanData?.type ?? span?.data?.type ?? span?.type ?? span?.name ?? "span").toLowerCase();
}
function eventType(type: string, phase: "start" | "end") {
  if (type.includes("function") || type.includes("tool")) return phase === "start" ? "tool_call.requested" : "tool_call.completed";
  if (type.includes("generation") || type.includes("response") || type.includes("model")) return phase === "start" ? "llm.request.started" : "llm.request";
  if (type.includes("handoff")) return phase === "start" ? "agent.handoff.started" : "agent.handoff.completed";
  return phase === "start" ? "agent.step.started" : "agent.step.completed";
}

export class SendaArgusOpenAIAgentsProcessor {
  async onSpanStart(span: any): Promise<void> {
    const type = spanType(span);
    emitEvent(eventType(type, "start"), { source: { component: "integration", framework: "openai-agents", sdk: "openai-agents", operation: type }, data: { agent: { span_id: span?.spanId ?? span?.span_id, trace_id: span?.traceId ?? span?.trace_id, span_hash: sha256Value(span?.toJSON?.() ?? span) } }, status: "started" });
  }
  async onSpanEnd(span: any): Promise<void> {
    const type = spanType(span);
    const failed = Boolean(span?.error);
    let evt = eventType(type, "end");
    if (failed) {
      if (evt === "tool_call.completed") evt = "tool_call.failed";
      else if (evt === "llm.request") evt = "llm.error";
      else evt = "agent.step.failed";
    }
    emitEvent(evt, { source: { component: "integration", framework: "openai-agents", sdk: "openai-agents", operation: type }, data: { agent: { span_id: span?.spanId ?? span?.span_id, trace_id: span?.traceId ?? span?.trace_id, span_hash: sha256Value(span?.toJSON?.() ?? span) } }, status: failed ? "error" : "success", error: failed ? { message: String(span.error?.message ?? span.error) } : undefined });
  }
  async forceFlush(): Promise<void> {}
}

export function instrumentOpenAIAgents(sdk: any): boolean {
  if (!sdk || typeof sdk.addTraceProcessor !== "function") return false;
  sdk.addTraceProcessor(new SendaArgusOpenAIAgentsProcessor());
  return true;
}
