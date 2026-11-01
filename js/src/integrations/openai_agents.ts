import { sha256Value } from "../core/hashing.js";
import { newTraceId, runWithContext } from "../core/context.js";
import { emitEvent, getConfig } from "../runtime.js";
import { argumentsScanFields, resultScanFields } from "../core/result_scan.js";

function spanType(span: any): string {
  return String(span?.spanData?.type ?? span?.data?.type ?? span?.type ?? span?.name ?? "span").toLowerCase();
}
function spanToolName(span: any): string {
  const name = span?.spanData?.name ?? span?.span_data?.name ?? span?.data?.name;
  return typeof name === "string" ? name.trim() : "";
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
    const evt = eventType(type, "start");
    const data: Record<string, unknown> = { agent: { span_id: span?.spanId ?? span?.span_id, trace_id: traceId, span_hash: sha256Value(span?.toJSON?.() ?? span) } };
    const toolName = spanToolName(span);
    const startTool: Record<string, unknown> = {};
    if (evt.startsWith("tool_call.") && toolName) startTool.tool_name = toolName;
    // function span の入力は、span の開始の後に設定される。引数が完了の記録で届くことを、付帯で示す。
    if (evt.startsWith("tool_call.") && toolName && getConfig().scanArguments) startTool.arguments_deferred = true;
    if (Object.keys(startTool).length > 0) data.tool = { framework: "openai-agents", ...startTool };
    runWithContext({ traceId }, () => emitEvent(evt, {
      source: { component: "integration", framework: "openai-agents", sdk: "openai-agents", operation: type },
      data,
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
    const data: Record<string, unknown> = { agent: { span_id: span?.spanId ?? span?.span_id, trace_id: traceId, span_hash: sha256Value(span?.toJSON?.() ?? span) } };
    // tool の呼び出しは、他の計装と同じ data.tool.tool_name に名前を置く。取り出せないときは載せない。
    const toolName = spanToolName(span);
    const tool: Record<string, unknown> = {};
    if (evt.startsWith("tool_call.") && toolName) tool.tool_name = toolName;
    // 開始の記録と同じ付帯を載せ、完了と失敗の記録で、引数の走査の文を送る。
    if (evt.startsWith("tool_call.") && toolName && getConfig().scanArguments) {
      tool.arguments_deferred = true;
      if (evt === "tool_call.completed" || evt === "tool_call.failed") {
        const input = span?.spanData?.input ?? span?.data?.input;
        if (input !== undefined && input !== null) Object.assign(tool, argumentsScanFields(input));
      }
    }
    // tool の区間の完了では、戻り値に埋め込まれた指示が注入の規則に届くよう走査の文を添える。
    if (evt === "tool_call.completed" && getConfig().scanResult) {
      const output = span?.spanData?.output ?? span?.data?.output;
      if (output !== undefined && output !== null) Object.assign(tool, resultScanFields(output));
    }
    if (Object.keys(tool).length > 0) data.tool = { framework: "openai-agents", ...tool };
    runWithContext({ traceId }, () => emitEvent(evt, {
      source: { component: "integration", framework: "openai-agents", sdk: "openai-agents", operation: type },
      data,
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
