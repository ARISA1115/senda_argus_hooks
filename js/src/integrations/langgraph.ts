import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { emitEvent } from "../runtime.js";

const patched = Symbol.for("senda.argus.langgraph.patched");

export async function invokeWithArgus(graph: any, input: any, config?: any) {
  const started = performance.now();
  emitEvent("agent.run.started", { source: { component: "integration", framework: "langgraph", sdk: "langgraph", operation: "invoke" }, data: { agent: { input_hash: sha256Value(input) } }, status: "started" });
  try {
    const result = await graph.invoke(input, config);
    emitEvent("agent.run.completed", { source: { component: "integration", framework: "langgraph", sdk: "langgraph", operation: "invoke" }, data: { agent: { output_hash: sha256Value(result) } }, status: "success", latencyMs: Math.round(performance.now() - started) });
    return result;
  } catch (error: any) {
    emitEvent("agent.run.failed", { source: { component: "integration", framework: "langgraph", sdk: "langgraph", operation: "invoke" }, status: "error", latencyMs: Math.round(performance.now() - started), error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) } });
    throw error;
  }
}

export async function* streamWithArgus(graph: any, input: any, config?: any): AsyncGenerator<any> {
  const started = performance.now();
  emitEvent("agent.run.started", { source: { component: "integration", framework: "langgraph", sdk: "langgraph", operation: "stream" }, data: { agent: { input_hash: sha256Value(input) } }, status: "started" });
  let step = 0;
  try {
    const stream = await graph.stream(input, config);
    for await (const chunk of stream) {
      step += 1;
      emitEvent("agent.step.completed", { source: { component: "integration", framework: "langgraph", sdk: "langgraph", operation: "stream" }, data: { agent: { step, chunk_hash: sha256Value(chunk) } }, status: "success" });
      yield chunk;
    }
    emitEvent("agent.run.completed", { source: { component: "integration", framework: "langgraph", sdk: "langgraph", operation: "stream" }, data: { agent: { steps: step } }, status: "success", latencyMs: Math.round(performance.now() - started) });
  } catch (error: any) {
    emitEvent("agent.run.failed", { source: { component: "integration", framework: "langgraph", sdk: "langgraph", operation: "stream" }, status: "error", latencyMs: Math.round(performance.now() - started), error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) } });
    throw error;
  }
}

export function instrumentLangGraph(graph: any): boolean {
  if (!graph || graph[patched]) return false;
  let changed = false;
  if (typeof graph.invoke === "function") {
    const original = graph.invoke.bind(graph);
    graph.invoke = (input: any, config?: any) => invokeWithArgus({ invoke: original }, input, config);
    changed = true;
  }
  if (typeof graph.stream === "function") {
    const original = graph.stream.bind(graph);
    graph.stream = (input: any, config?: any) => streamWithArgus({ stream: original }, input, config);
    changed = true;
  }
  if (changed) graph[patched] = true;
  return changed;
}
