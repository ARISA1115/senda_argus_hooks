import type { RegisterOptions } from "./core/types.js";
import { configure } from "./runtime.js";
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
  return instrument(targets);
}

export function instrument(targets: Targets = {}) {
  return {
    openai: targets.openai ? instrumentOpenAI(targets.openai) : false,
    anthropic: targets.anthropic ? instrumentAnthropic(targets.anthropic) : false,
    ollama: targets.ollama ? instrumentOllama(targets.ollama) : false,
    mcp: targets.mcp ? instrumentMCP(targets.mcp, targets.mcpMetadata) : false,
    langgraph: targets.langgraph ? instrumentLangGraph(targets.langgraph) : false,
    llamaindex: targets.llamaindex ? instrumentLlamaIndex(targets.llamaindex) : false,
    openaiAgents: targets.openaiAgents ? instrumentOpenAIAgents(targets.openaiAgents) : false
  };
}
