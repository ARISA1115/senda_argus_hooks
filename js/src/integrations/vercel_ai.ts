import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { emitEvent, getConfig } from "../runtime.js";
import { safeValue } from "../instrumentors/common.js";

function llmData(params: any, model: any, result?: any) {
  const cfg = getConfig();
  const provider = model?.provider ?? model?.providerId ?? "vercel-ai";
  const modelId = model?.modelId ?? model?.model_id;
  return {
    provider, model: modelId,
    input: cfg.capturePrompt ? safeValue(params) : { input_hash: sha256Value(params) },
    ...(result === undefined ? {} : { output: cfg.captureResponse ? safeValue(result) : { response_hash: sha256Value(result) } })
  };
}

export function sendaArgusLanguageModelMiddleware(): any {
  return {
    specificationVersion: "v3",
    wrapGenerate: async ({ doGenerate, params, model }: any) => {
      const started = performance.now();
      try {
        const result = await doGenerate();
        emitEvent("llm.request", { source: { component: "integration", framework: "vercel-ai-sdk", sdk: "ai", provider: model?.provider, operation: "doGenerate" }, data: { llm: llmData(params, model, result) }, status: "success", latencyMs: Math.round(performance.now() - started) });
        return result;
      } catch (error: any) {
        emitEvent("llm.error", { source: { component: "integration", framework: "vercel-ai-sdk", sdk: "ai", provider: model?.provider, operation: "doGenerate" }, data: { llm: llmData(params, model) }, status: "error", latencyMs: Math.round(performance.now() - started), error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) } });
        throw error;
      }
    },
    wrapStream: async ({ doStream, params, model }: any) => {
      const started = performance.now();
      try {
        const result = await doStream();
        emitEvent("llm.request", { source: { component: "integration", framework: "vercel-ai-sdk", sdk: "ai", provider: model?.provider, operation: "doStream" }, data: { llm: llmData(params, model, { streaming: true }) }, status: "success", latencyMs: Math.round(performance.now() - started) });
        return result;
      } catch (error: any) {
        emitEvent("llm.error", { source: { component: "integration", framework: "vercel-ai-sdk", sdk: "ai", provider: model?.provider, operation: "doStream" }, data: { llm: llmData(params, model) }, status: "error", latencyMs: Math.round(performance.now() - started), error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) } });
        throw error;
      }
    }
  };
}
