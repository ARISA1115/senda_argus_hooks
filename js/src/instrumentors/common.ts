import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { emitEvent, getConfig, observe } from "../runtime.js";
import { offeredAlternatives } from "../core/mcp_tools.js";

type AnyFn = (...args: any[]) => any;
const patched = Symbol.for("senda.argus.patched");

export function patchAsyncMethod(target: any, method: string, opts: {
  sdk: string; provider: string; operation: string;
  input: (args: any[]) => Record<string, unknown>;
  output?: (value: any) => Record<string, unknown>;
}): boolean {
  if (!target || typeof target[method] !== "function") return false;
  const original: AnyFn & { [patched]?: boolean } = target[method];
  if (original[patched]) return false;

  const wrapped: AnyFn & { [patched]?: boolean } = function(this: unknown, ...args: any[]) {
    const cfg = getConfig();
    const started = performance.now();
    let base: Record<string, unknown> = {};
    observe(() => { base = opts.input(args); });
    const emitSuccess = (value: any) => observe(() => {
      const output = opts.output?.(value) ?? { response_hash: sha256Value(safeValue(value)) };
      emitEvent("llm.request", {
        source: { component: "instrumentor", sdk: opts.sdk, provider: opts.provider, operation: opts.operation },
        data: { llm: { provider: opts.provider, operation: opts.operation, ...base, output: cfg.captureResponse ? safeValue(value) : output } },
        status: "success", latencyMs: Math.round(performance.now() - started)
      });
      emitToolDecisions(args, value, opts, started);
    });
    let result: any;
    try {
      result = original.apply(this, args);
    } catch (error) {
      emitError(error, base, opts, started); throw error;
    }
    if (result && typeof result.then === "function") {
      return result.then((value: any) => {
        emitSuccess(value);
        return value;
      }, (error: any) => {
        emitError(error, base, opts, started); throw error;
      });
    }
    emitSuccess(result);
    return result;
  };
  wrapped[patched] = true;
  target[method] = wrapped;
  return true;
}

// LLM に差し出したツールとモデルが選んだツールを、選んだツールごとに 1 件の agent.decision で送る。
// Python の計装と同じ形で、候補は MCP の一覧から引けたものにだけサーバを付ける。Argus の選択誘導の
// 検知はこの 2 つを読む。候補か選択のどちらかが無ければ送らない。
function emitToolDecisions(args: any[], value: any, opts: { sdk: string; provider: string; operation: string }, started: number) {
  const offered = offeredToolNames(args[0]);
  if (!offered.length) return;
  const selected = selectedToolNames(value);
  if (!selected.length) return;
  const alternatives = offeredAlternatives(offered);
  for (const selectedTool of selected) {
    emitEvent("agent.decision", {
      source: { component: "instrumentor", sdk: opts.sdk, provider: opts.provider, operation: opts.operation },
      data: { alternatives, selected_tool: selectedTool },
      status: "success", latencyMs: Math.round(performance.now() - started)
    });
  }
}

// 呼び出しの引数からツールの名前を取り出す。chat.completions は tools[].function.name、
// responses と Anthropic の messages は tools[].name に置く。
export function offeredToolNames(payload: any): string[] {
  const tools = payload?.tools;
  if (!Array.isArray(tools)) return [];
  const names: string[] = [];
  for (const tool of tools) {
    const name = tool?.function?.name ?? tool?.name;
    if (typeof name === "string" && name) names.push(name);
  }
  return names;
}

// 応答からモデルが選んだツールの名前を順に全部取り出す。並列の呼び出しでは複数入る。
// chat.completions は choices[].message.tool_calls[].function.name、responses は output[] の
// function_call の name、Anthropic は content[] の tool_use の name に置く。
export function selectedToolNames(value: any): string[] {
  const names: string[] = [];
  const push = (name: unknown) => { if (typeof name === "string" && name) names.push(name); };
  if (Array.isArray(value?.choices)) {
    for (const choice of value.choices) {
      const calls = choice?.message?.tool_calls;
      if (Array.isArray(calls)) for (const call of calls) push(call?.function?.name);
    }
  }
  if (Array.isArray(value?.output)) {
    for (const item of value.output) if (item?.type === "function_call") push(item?.name);
  }
  if (Array.isArray(value?.content)) {
    for (const block of value.content) if (block?.type === "tool_use") push(block?.name);
  }
  return names;
}

function emitError(error: any, base: Record<string, unknown>, opts: any, started: number) {
  observe(() => emitEvent("llm.error", {
    source: { component: "instrumentor", sdk: opts.sdk, provider: opts.provider, operation: opts.operation },
    data: { llm: { provider: opts.provider, operation: opts.operation, ...base } },
    status: "error", latencyMs: Math.round(performance.now() - started),
    error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) }
  }));
}

export function llmInput(args: any[], payloadIndex = 0): Record<string, unknown> {
  const cfg = getConfig();
  const payload = args[payloadIndex] ?? {};
  const model = payload?.model;
  const messages = payload?.messages ?? payload?.input;
  const input = cfg.capturePrompt ? payload : { input_hash: sha256Value(payload) };
  const out: Record<string, unknown> = { model, input };
  if (messages !== undefined && cfg.captureHash) {
    out.messages_hash = sha256Value(messages);
    if (Array.isArray(messages)) out.messages_count = messages.length;
  }
  return out;
}

export function safeValue(value: any): unknown {
  if (value == null) return value;
  if (typeof value.toJSON === "function") { try { return value.toJSON(); } catch {} }
  if (typeof value.model_dump === "function") { try { return value.model_dump(); } catch {} }
  if (typeof value === "object") {
    try { return JSON.parse(JSON.stringify(value)); } catch { return String(value); }
  }
  return value;
}
