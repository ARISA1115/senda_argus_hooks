import type { RegisterOptions } from "./core/types.js";
import { configure } from "./runtime.js";
import { instrumentOpenAI } from "./instrumentors/openai.js";
import { instrumentAnthropic } from "./instrumentors/anthropic.js";
import { instrumentOllama } from "./instrumentors/ollama.js";
import { instrumentMCP } from "./instrumentors/mcp.js";

export interface Targets {
  openai?: any;
  anthropic?: any;
  ollama?: any;
  mcp?: any;
  mcpMetadata?: { serverName?: string; serverUrl?: string; capability?: string };
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
    mcp: targets.mcp ? instrumentMCP(targets.mcp, targets.mcpMetadata) : false
  };
}
