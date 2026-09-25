import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire, register as registerLoader } from "node:module";
import { register } from "../register.js";
import { shutdown } from "../runtime.js";
import { instrumentOpenAI } from "../instrumentors/openai.js";
import { instrumentAnthropic } from "../instrumentors/anthropic.js";
import { instrumentOllama } from "../instrumentors/ollama.js";
import { instrumentMCP } from "../instrumentors/mcp.js";
import { instrumentOpenAIAgents } from "../integrations/openai_agents.js";

const truthy = (v: unknown, fallback = false): boolean => {
  if (v == null || v === "") return fallback;
  return /^(1|true|yes|on)$/i.test(String(v));
};
const enabledValue = (v: unknown, fallback = true): boolean => {
  if (v == null || v === "") return fallback;
  return !/^(0|false|no|off)$/i.test(String(v));
};

function parseEnvFile(file: string): Record<string, string> {
  const out: Record<string, string> = {};
  try {
    const text = fs.readFileSync(file, "utf8");
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line || line.startsWith("#")) continue;
      const idx = line.indexOf("=");
      if (idx < 1) continue;
      const key = line.slice(0, idx).trim();
      let value = line.slice(idx + 1).trim();
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      out[key] = value;
    }
  } catch {}
  return out;
}

function loadExternalEnv(): void {
  const candidates = [
    process.env.SENDA_ARGUS_ENV_FILE,
    "/etc/senda-argus/node.env",
    "/etc/senda-argus/hooks.env",
    path.join(os.homedir(), ".config", "senda-argus", "node.env"),
    path.join(os.homedir(), ".config", "senda-argus", "hooks.env"),
  ].filter(Boolean) as string[];
  for (const file of candidates) {
    if (!fs.existsSync(file)) continue;
    const values = parseEnvFile(file);
    for (const [key, value] of Object.entries(values)) {
      if (process.env[key] === undefined) process.env[key] = value;
    }
    break;
  }
}

function exportersFromEnv(): any[] {
  const names = String(process.env.SENDA_ARGUS_EXPORTERS ?? process.env.SENDA_ARGUS_EXPORTER ?? "jsonl")
    .split(",").map((v) => v.trim()).filter(Boolean);
  return names.map((name) => {
    if (name === "stdout") return { type: "stdout" };
    if (name === "null") return { type: "null" };
    if (name === "argus") return { type: "argus", endpoint: process.env.SENDA_ARGUS_ENDPOINT ?? "http://localhost:8000", apiKey: process.env.SENDA_ARGUS_API_KEY ?? "", runId: process.env.SENDA_ARGUS_RUN_ID, timeoutMs: Number(process.env.SENDA_ARGUS_TIMEOUT_MS ?? "10000") };
    return { type: "jsonl", path: process.env.SENDA_ARGUS_JSONL_PATH ?? "/var/log/senda-argus/events.jsonl" };
  });
}


function wrapCommonJsValue(request: string, value: any): any {
  try {
    if (request === "openai") {
      const Base = value?.default ?? value?.OpenAI ?? value;
      if (typeof Base === "function" && !(Base as any).__sendaArgusWrapped) {
        class Wrapped extends Base { constructor(...args: any[]) { super(...args); try { instrumentOpenAI(this); } catch {} } }
        (Wrapped as any).__sendaArgusWrapped = true;
        if (value && (typeof value === "object" || typeof value === "function")) {
          try { value.default = Wrapped; } catch {}
          try { value.OpenAI = Wrapped; } catch {}
          if (typeof value === "function") return Object.assign(Wrapped, value);
          return value;
        }
        return Wrapped;
      }
    } else if (request === "@anthropic-ai/sdk") {
      const Base = value?.default ?? value?.Anthropic ?? value;
      if (typeof Base === "function" && !(Base as any).__sendaArgusWrapped) {
        class Wrapped extends Base { constructor(...args: any[]) { super(...args); try { instrumentAnthropic(this); } catch {} } }
        (Wrapped as any).__sendaArgusWrapped = true;
        if (value && (typeof value === "object" || typeof value === "function")) {
          try { value.default = Wrapped; } catch {}
          try { value.Anthropic = Wrapped; } catch {}
          if (typeof value === "function") return Object.assign(Wrapped, value);
          return value;
        }
        return Wrapped;
      }
    } else if (request === "ollama") {
      instrumentOllama(value?.default ?? value);
    } else if (request === "@modelcontextprotocol/sdk/client/index.js") {
      const Base = value?.Client;
      if (typeof Base === "function" && !(Base as any).__sendaArgusWrapped) {
        class WrappedClient extends Base { constructor(...args: any[]) { super(...args); try { instrumentMCP(this); } catch {} } }
        (WrappedClient as any).__sendaArgusWrapped = true;
        try { value.Client = WrappedClient; } catch {}
      }
    } else if (request === "@openai/agents") {
      instrumentOpenAIAgents(value);
    }
  } catch {}
  return value;
}

function eagerPatchCommonJsTargets(): void {
  const main = process.argv[1] ?? "";
  if (!(main.endsWith(".cjs") || truthy(process.env.SENDA_ARGUS_CJS_EAGER_PATCH))) return;
  let appRequire: any;
  try { appRequire = createRequire(path.join(process.cwd(), "package.json")); } catch { return; }
  for (const request of ["openai", "@anthropic-ai/sdk", "ollama", "@modelcontextprotocol/sdk/client/index.js", "@openai/agents"]) {
    try {
      const resolved = appRequire.resolve(request);
      const value = appRequire(request);
      const wrapped = wrapCommonJsValue(request, value);
      if (appRequire.cache?.[resolved]) appRequire.cache[resolved].exports = wrapped;
    } catch {}
  }
}

function patchCommonJs(): void {
  const require = createRequire(import.meta.url);
  const mod: any = require("node:module");
  if (!mod?._load || mod._load?.__sendaArgusPatched) return;
  const originalLoad = mod._load;
  const wrapped = function(this: any, request: string, parent: any, isMain: boolean) {
    const value = originalLoad.call(this, request, parent, isMain);
    return wrapCommonJsValue(request, value);
  };
  (wrapped as any).__sendaArgusPatched = true;
  mod._load = wrapped;
}


loadExternalEnv();
if (enabledValue(process.env.SENDA_ARGUS_ENABLED, true)) {
  try {
    register({
      project: process.env.SENDA_ARGUS_PROJECT ?? "default",
      environment: process.env.SENDA_ARGUS_ENVIRONMENT ?? "prod",
      exporters: exportersFromEnv(),
      capturePrompt: truthy(process.env.SENDA_ARGUS_CAPTURE_PROMPT),
      captureResponse: truthy(process.env.SENDA_ARGUS_CAPTURE_RESPONSE),
      captureArguments: truthy(process.env.SENDA_ARGUS_CAPTURE_ARGUMENTS),
      captureResult: truthy(process.env.SENDA_ARGUS_CAPTURE_RESULT),
      captureHash: enabledValue(process.env.SENDA_ARGUS_CAPTURE_HASH, true),
      redact: enabledValue(process.env.SENDA_ARGUS_REDACT, true),
      tenantId: process.env.SENDA_ARGUS_TENANT_ID,
      sessionId: process.env.SENDA_ARGUS_SESSION_ID,
      conversationId: process.env.SENDA_ARGUS_CONVERSATION_ID,
      runId: process.env.SENDA_ARGUS_RUN_ID,
      turnId: process.env.SENDA_ARGUS_TURN_ID,
      agentId: process.env.SENDA_ARGUS_AGENT_ID,
      purposeId: process.env.SENDA_ARGUS_PURPOSE_ID,
      agentHint: process.env.SENDA_ARGUS_AGENT_HINT,
    });
    patchCommonJs();
    eagerPatchCommonJsTargets();
    try { registerLoader(new URL("./loader.js", import.meta.url)); } catch {}
    try { process.once("beforeExit", () => { void shutdown(); }); } catch {}
    if (truthy(process.env.SENDA_ARGUS_BOOTSTRAP_DEBUG)) {
      console.error("[senda-argus] Node zero-code preload enabled");
    }
  } catch (error) {
    if (truthy(process.env.SENDA_ARGUS_BOOTSTRAP_DEBUG)) {
      console.error("[senda-argus] Node zero-code preload failed open:", error);
    }
  }
}
