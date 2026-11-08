import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { newTraceId, runWithContext } from "../core/context.js";
import { emitEvent, getConfig } from "../runtime.js";
import { safeValue } from "../instrumentors/common.js";
import { argumentsScanFields, resultScanFields } from "../core/result_scan.js";
import { checkToolCall, precheckEnabled } from "../core/precheck.js";
import { retrievedTextScanFields } from "./retrieval_scan.js";

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

// ツールの入力を構造のまま返す。JSON の文字列のままだと改行が 2 文字になり、2 行目の命令を読めない。
function structuredInput(input: unknown): unknown {
  if (typeof input !== "string" || !/^[{[]/.test(input)) return input;
  try {
    return JSON.parse(input);
  } catch {
    return input;
  }
}

function maybeBody(value: unknown, capture: boolean) {
  return capture ? safeValue(value) : { hash: sha256Value(safeValue(value)) };
}

// 本文を送らない既定でも、戻り値に埋め込まれた指示が注入の規則に届くよう、走査の文を添える。
function toolResult(output: unknown): Record<string, unknown> {
  const tool: Record<string, unknown> = { framework: "langchain", result: maybeBody(output, getConfig().captureResult) };
  if (getConfig().scanResult) {
    Object.assign(tool, resultScanFields(safeValue(output)));
  }
  return tool;
}

export class SendaArgusLangChainCallbackHandler {
  name = "senda_argus";
  // LangChain は raiseError の無いハンドラーの例外を握りつぶす。実行前の判定を有効にしたときだけ立てる。
  get raiseError(): boolean { return precheckEnabled(); }
  awaitHandlers = true;

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

  handleRetrieverStart(retriever: any, query: string, runId?: unknown) {
    const traceId = begin(runId);
    return runWithContext({ traceId, runId: key(runId) || undefined }, () => emitEvent("retrieval.requested", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "retriever" },
      data: { retrieval: { framework: "langchain", retriever_name: retriever?.name ?? retriever?.id?.at?.(-1), query_hash: sha256Value(query) } },
      status: "requested"
    }));
  }

  handleRetrieverEnd(documents: any, runId?: unknown) {
    const lifecycle = finish(runId);
    return runWithContext({ traceId: lifecycle.traceId, runId: key(runId) || undefined }, () => emitEvent("retrieval.completed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "retriever" },
      data: { retrieval: { framework: "langchain", result_count: Array.isArray(documents) ? documents.length : undefined, result_hash: sha256Value(documents), ...retrievedTextScanFields(documents) } },
      status: "success", latencyMs: lifecycle.latencyMs
    }));
  }

  handleRetrieverError(error: any, runId?: unknown) {
    const lifecycle = finish(runId);
    return runWithContext({ traceId: lifecycle.traceId, runId: key(runId) || undefined }, () => emitEvent("retrieval.failed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "retriever" },
      status: "error", latencyMs: lifecycle.latencyMs,
      error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) }
    }));
  }

  async handleToolStart(tool: any, input: any, runId?: unknown) {
    const traceId = begin(runId);
    // 記録の後に問い合わせる。止めた呼び出しも、試みとして記録に残す。
    await runWithContext({ traceId, runId: key(runId) || undefined }, () => emitEvent("tool_call.requested", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "tool" },
      data: { tool: { framework: "langchain", name: tool?.name ?? tool?.id?.at?.(-1), arguments: maybeBody(input, getConfig().captureArguments), ...(getConfig().scanArguments ? argumentsScanFields(structuredInput(input)) : {}) } },
      status: "requested"
    }));
    try {
      await checkToolCall(tool?.name ?? tool?.id?.at?.(-1), structuredInput(input), "langchain");
    } catch (error) {
      // 止めた呼び出しには終了の通知が来ない。控えを残すと、拒否のたびに増える。
      finish(runId);
      throw error;
    }
  }

  handleToolEnd(output: any, runId?: unknown) {
    const lifecycle = finish(runId);
    return runWithContext({ traceId: lifecycle.traceId, runId: key(runId) || undefined }, () => emitEvent("tool_call.completed", {
      source: { component: "integration", framework: "langchain", sdk: "langchain", operation: "tool" },
      data: { tool: toolResult(output) },
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

// raiseError はハンドラーの全コールバックに効くため、実行の判定以外の観測の例外は止める。
for (const name of Object.getOwnPropertyNames(SendaArgusLangChainCallbackHandler.prototype)) {
  if (!name.startsWith("handle") || name === "handleToolStart") continue;
  const proto = SendaArgusLangChainCallbackHandler.prototype as any;
  const original = proto[name];
  proto[name] = function (this: unknown, ...args: unknown[]) {
    try {
      const result = original.apply(this, args);
      return result && typeof result.catch === "function" ? result.catch(() => undefined) : result;
    } catch {
      return undefined;
    }
  };
}

export function langChainCallbackHandler() {
  return new SendaArgusLangChainCallbackHandler();
}
