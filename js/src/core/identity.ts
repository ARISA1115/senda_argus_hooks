import { stableHash } from "./hashing.js";

export function normalizeUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    url.hash = "";
    url.search = "";
    url.hostname = url.hostname.toLowerCase();
    url.protocol = url.protocol.toLowerCase();
    url.pathname = url.pathname.replace(/\/+$/, "") || "/";
    return url.toString();
  } catch {
    return value.trim().toLowerCase().replace(/\/+$/, "");
  }
}

export function deriveAgentId(project: string, environment: string, sdk?: string, agentHint?: string): string {
  return stableHash({ project, environment, sdk: sdk ?? "unknown", agent_hint: agentHint ?? "default" }, "agent");
}

export function mcpDataSourceProfile(serverName?: string, serverUrl?: string | null, toolName?: string, capability?: string) {
  return {
    source_type: "mcp",
    mcp_server_name: serverName ?? "unknown",
    mcp_server_url: normalizeUrl(serverUrl),
    tool_name: toolName ?? "unknown",
    capability: capability ?? "unknown"
  };
}

export function derivePurposeId(serverName?: string, serverUrl?: string | null, toolName?: string, capability?: string): string {
  return stableHash(mcpDataSourceProfile(serverName, serverUrl, toolName, capability), "purpose");
}

export function deriveMcpProfileId(serverName?: string, serverUrl?: string | null): string {
  return stableHash({ mcp_server_name: serverName ?? "unknown", mcp_server_url: normalizeUrl(serverUrl), tools: [] }, "mcp_profile");
}

export function dataSourceHash(profile: unknown): string {
  return stableHash(profile, "data_source");
}
