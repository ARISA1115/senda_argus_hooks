import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { emitEvent, getConfig } from "../runtime.js";

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
    const base = opts.input(args);
    try {
      const result = original.apply(this, args);
      if (result && typeof result.then === "function") {
        return result.then((value: any) => {
          const output = opts.output?.(value) ?? { response_hash: sha256Value(safeValue(value)) };
          emitEvent("llm.request", {
            source: { component: "instrumentor", sdk: opts.sdk, provider: opts.provider, operation: opts.operation },
            data: { llm: { provider: opts.provider, operation: opts.operation, ...base, output: cfg.captureResponse ? safeValue(value) : output } },
            status: "success", latencyMs: Math.round(performance.now() - started)
          });
          return value;
        }, (error: any) => {
          emitError(error, base, opts, started); throw error;
        });
      }
      const output = opts.output?.(result) ?? { response_hash: sha256Value(safeValue(result)) };
      emitEvent("llm.request", {
        source: { component: "instrumentor", sdk: opts.sdk, provider: opts.provider, operation: opts.operation },
        data: { llm: { provider: opts.provider, operation: opts.operation, ...base, output: cfg.captureResponse ? safeValue(result) : output } },
        status: "success", latencyMs: Math.round(performance.now() - started)
      });
      return result;
    } catch (error) {
      emitError(error, base, opts, started); throw error;
    }
  };
  wrapped[patched] = true;
  target[method] = wrapped;
  return true;
}

function emitError(error: any, base: Record<string, unknown>, opts: any, started: number) {
  emitEvent("llm.error", {
    source: { component: "instrumentor", sdk: opts.sdk, provider: opts.provider, operation: opts.operation },
    data: { llm: { provider: opts.provider, operation: opts.operation, ...base } },
    status: "error", latencyMs: Math.round(performance.now() - started),
    error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) }
  });
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
