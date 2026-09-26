const WRAPPED = new Map<string, string>([
  ["openai", "openai"],
  ["@anthropic-ai/sdk", "anthropic"],
  ["ollama", "ollama"],
  ["@modelcontextprotocol/sdk/client/index.js", "mcp"],
  ["@openai/agents", "openai_agents"],
]);

const instrumentorUrls = {
  openai: new URL("../instrumentors/openai.js", import.meta.url).href,
  anthropic: new URL("../instrumentors/anthropic.js", import.meta.url).href,
  ollama: new URL("../instrumentors/ollama.js", import.meta.url).href,
  mcp: new URL("../instrumentors/mcp.js", import.meta.url).href,
  openai_agents: new URL("../integrations/openai_agents.js", import.meta.url).href,
};

function synthetic(kind: string, originalUrl: string): string {
  return `senda-argus:${kind}?url=${encodeURIComponent(originalUrl)}`;
}

function sourceFor(kind: string, originalUrl: string): string {
  const original = `${originalUrl}${originalUrl.includes("?") ? "&" : "?"}senda_argus_original=1`;
  switch (kind) {
    case "openai":
      return `
        import OriginalDefault from ${JSON.stringify(original)};
        export * from ${JSON.stringify(original)};
        import { instrumentOpenAI } from ${JSON.stringify(instrumentorUrls.openai)};
        class SendaArgusOpenAI extends OriginalDefault {
          constructor(...args) { super(...args); try { instrumentOpenAI(this); } catch {} }
        }
        export { SendaArgusOpenAI as OpenAI };
        export default SendaArgusOpenAI;
      `;
    case "anthropic":
      return `
        import OriginalDefault from ${JSON.stringify(original)};
        export * from ${JSON.stringify(original)};
        import { instrumentAnthropic } from ${JSON.stringify(instrumentorUrls.anthropic)};
        class SendaArgusAnthropic extends OriginalDefault {
          constructor(...args) { super(...args); try { instrumentAnthropic(this); } catch {} }
        }
        export { SendaArgusAnthropic as Anthropic };
        export default SendaArgusAnthropic;
      `;
    case "ollama":
      return `
        import OriginalDefault from ${JSON.stringify(original)};
        export * from ${JSON.stringify(original)};
        import { instrumentOllama } from ${JSON.stringify(instrumentorUrls.ollama)};
        try { instrumentOllama(OriginalDefault); } catch {}
        export default OriginalDefault;
      `;
    case "mcp":
      return `
        import * as Original from ${JSON.stringify(original)};
        export * from ${JSON.stringify(original)};
        import { instrumentMCP } from ${JSON.stringify(instrumentorUrls.mcp)};
        class SendaArgusClient extends Original.Client {
          constructor(...args) { super(...args); try { instrumentMCP(this); } catch {} }
        }
        export { SendaArgusClient as Client };
      `;
    case "openai_agents":
      return `
        import * as Original from ${JSON.stringify(original)};
        export * from ${JSON.stringify(original)};
        import { instrumentOpenAIAgents } from ${JSON.stringify(instrumentorUrls.openai_agents)};
        try { instrumentOpenAIAgents(Original); } catch {}
      `;
    default:
      return `export * from ${JSON.stringify(original)};`;
  }
}

export async function resolve(specifier: string, context: any, nextResolve: any) {
  if (specifier.startsWith("senda-argus:") || specifier.includes("senda_argus_original=1")) {
    return nextResolve(specifier, context);
  }
  const kind = WRAPPED.get(specifier);
  if (!kind) return nextResolve(specifier, context);
  const resolved = await nextResolve(specifier, context);
  return { url: synthetic(kind, resolved.url), shortCircuit: true };
}

export async function load(url: string, context: any, nextLoad: any) {
  if (!url.startsWith("senda-argus:")) return nextLoad(url, context);
  const match = /^senda-argus:([^?]+)\?url=(.*)$/.exec(url);
  if (!match) return nextLoad(url, context);
  const kind = match[1];
  const originalUrl = decodeURIComponent(match[2]);
  return { format: "module", source: sourceFor(kind, originalUrl), shortCircuit: true };
}
