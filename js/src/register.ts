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
  if (Object.values(result).some(Boolean)) markInstrumented();
  return result;
}
