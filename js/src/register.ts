import type { RegisterOptions } from "./core/types.js";
import { configure, markInstrumented } from "./runtime.js";
import { autostartDisabled, startCanary } from "./onboarding.js";
import { instrumentOpenAI } from "./instrumentors/openai.js";
import { instrumentAnthropic } from "./instrumentors/anthropic.js";
import { instrumentOllama } from "./instrumentors/ollama.js";
import { instrumentMCP } from "./instrumentors/mcp.js";
import { instrumentLangGraph } from "./integrations/langgraph.js";
import { instrumentLlamaIndex } from "./integrations/llamaindex.js";
import { instrumentOpenAIAgents } from "./integrations/openai_agents.js";

export interface Targets {
  openai?: any;
  anthropic?: any;
  ollama?: any;
  mcp?: any;
  mcpMetadata?: { serverName?: string; serverUrl?: string; capability?: string };
  langgraph?: any;
  llamaindex?: { retriever?: any; embedModel?: any; queryEngine?: any };
  openaiAgents?: any;
}

export function register(options: RegisterOptions = {}, targets: Targets = {}) {
  configure(options);
  const result = instrument(targets);
  // shutdown で止めた canary を、収集を再開したときに始め直す。鍵が設定されていなければ何もしない。
  if (!autostartDisabled()) {
    try { startCanary(); } catch { /* 観測の失敗で登録を止めない */ }
  }
  return result;
}

export function instrument(targets: Targets = {}) {
  const result = {
    openai: targets.openai ? instrumentOpenAI(targets.openai) : false,
    anthropic: targets.anthropic ? instrumentAnthropic(targets.anthropic) : false,
    ollama: targets.ollama ? instrumentOllama(targets.ollama) : false,
    mcp: targets.mcp ? instrumentMCP(targets.mcp, targets.mcpMetadata) : false,
    langgraph: targets.langgraph ? instrumentLangGraph(targets.langgraph) : false,
    llamaindex: targets.llamaindex ? instrumentLlamaIndex(targets.llamaindex) : false,
    openaiAgents: targets.openaiAgents ? instrumentOpenAIAgents(targets.openaiAgents) : false
  };
  // shutdown の後に同じ呼び出し先を登録し直すと、包んだ関数が既にあるため計装はどれも新しく入らない。
  // 包んだ関数は新しい exporter へ送り続けるので、既に包まれた呼び出し先も計装が入っているとみなす。
  // みなさないと、登録し直した後の canary が送られず、収集の停止に見える。
  if (Object.values(result).some(Boolean) || Object.values(targets).some((t) => hasPatchedFunction(t))) {
    markInstrumented();
  }
  return result;
}

const PATCHED_MARKERS = [Symbol.for("senda.argus.patched"), Symbol.for("senda.argus.mcp.patched")];

// 呼び出し先の中に、計装が包んだ関数があるか。クライアントは chat.completions.create のように入れ子に
// なるため、決まった深さまでたどる。
function hasPatchedFunction(target: unknown, depth = 4, seen = new Set<unknown>()): boolean {
  if (!target || (typeof target !== "object" && typeof target !== "function") || seen.has(target)) return false;
  seen.add(target);
  if (typeof target === "function" && PATCHED_MARKERS.some((m) => (target as any)[m])) return true;
  if (depth <= 0) return false;
  const names = new Set<string>();
  for (let o: any = target; o && o !== Object.prototype && o !== Function.prototype; o = Object.getPrototypeOf(o)) {
    for (const name of Object.getOwnPropertyNames(o)) names.add(name);
  }
  for (const name of names) {
    if (name === "constructor") continue;
    let value: unknown;
    try { value = (target as any)[name]; } catch { continue; }
    if (hasPatchedFunction(value, depth - 1, seen)) return true;
  }
  return false;
}
