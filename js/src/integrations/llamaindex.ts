import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { emitEvent, getConfig } from "../runtime.js";
import { safeValue } from "../instrumentors/common.js";

const patched = Symbol.for("senda.argus.llamaindex.patched");

function patch(target: any, method: string, kind: "retrieval" | "embedding" | "rag.query"): boolean {
  if (!target || typeof target[method] !== "function") return false;
  const original = target[method];
  if (original[patched]) return false;
  const wrapped = async function(this: any, ...args: any[]) {
    const started = performance.now();
    const startEvent = kind === "rag.query" ? "rag.query.started" : `${kind}.requested`;
    emitEvent(startEvent, {
      source: { component: "integration", framework: "llamaindex", sdk: "llamaindex", operation: method },
      data: { [kind === "rag.query" ? "rag" : kind]: { framework: "llamaindex", operation: method, input_hash: sha256Value(args) } }, status: "started"
    });
    try {
      const result = await original.apply(this, args);
      emitEvent(`${kind}.completed`, {
        source: { component: "integration", framework: "llamaindex", sdk: "llamaindex", operation: method },
        data: { [kind === "rag.query" ? "rag" : kind]: { framework: "llamaindex", operation: method, result: getConfig().captureResult ? safeValue(result) : { result_hash: sha256Value(result) } } },
        status: "success", latencyMs: Math.round(performance.now() - started)
      });
      return result;
    } catch (error: any) {
      emitEvent(`${kind}.failed`, { source: { component: "integration", framework: "llamaindex", sdk: "llamaindex", operation: method }, status: "error", latencyMs: Math.round(performance.now() - started), error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) } });
      throw error;
    }
  };
  wrapped[patched] = true;
  target[method] = wrapped;
  return true;
}

export function instrumentLlamaIndex(options: { retriever?: any; embedModel?: any; queryEngine?: any }): boolean {
  let changed = false;
  for (const method of ["retrieve", "aretrieve"]) changed = patch(options.retriever, method, "retrieval") || changed;
  for (const method of ["getTextEmbedding", "getTextEmbeddings", "getQueryEmbedding", "getTextEmbeddingBatch"]) changed = patch(options.embedModel, method, "embedding") || changed;
  for (const method of ["query", "aquery"]) changed = patch(options.queryEngine, method, "rag.query") || changed;
  return changed;
}
