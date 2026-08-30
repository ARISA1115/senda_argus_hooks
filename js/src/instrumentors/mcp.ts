import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { dataSourceHash, deriveMcpProfileId, derivePurposeId, mcpDataSourceProfile, normalizeUrl } from "../core/identity.js";
import { emitEvent, getConfig } from "../runtime.js";

const patched = Symbol.for("senda.argus.mcp.patched");

export function instrumentMCP(client: any, metadata: { serverName?: string; serverUrl?: string; capability?: string } = {}): boolean {
  if (!client || typeof client.callTool !== "function" || client.callTool[patched]) return false;
  const original = client.callTool;
  const wrapped = async function(this: unknown, request: any, ...rest: any[]) {
    const started = performance.now();
    const cfg = getConfig();
    const toolName = request?.name ?? "unknown";
    const serverName = metadata.serverName ?? client.serverName ?? client.name ?? "unknown";
    const serverUrl = metadata.serverUrl ?? client.serverUrl ?? client.url ?? client.baseUrl ?? null;
    const capability = metadata.capability ?? client.capability;
    const purposeProfile = mcpDataSourceProfile(serverName, serverUrl, toolName, capability);
    const purposeId = derivePurposeId(serverName, serverUrl, toolName, capability);
    const meta: Record<string, unknown> = {
      operation: "callTool", server: serverName, server_url: normalizeUrl(serverUrl), tool: toolName,
      capability, mcp_profile_id: deriveMcpProfileId(serverName, serverUrl), purpose_id: purposeId,
      purpose_source: "mcp_data_source_hash", purpose_profile: purposeProfile,
      data_source_hash: dataSourceHash(purposeProfile), arguments_hash: sha256Value(request)
    };
    if (cfg.captureArguments) meta.arguments = request;
    emitEvent("mcp.tool_call.requested", { source: { component: "instrumentor", sdk: "mcp_js", operation: "callTool" }, data: { mcp: meta }, status: "start", purposeId });
    try {
      const result = await original.call(this, request, ...rest);
      const completed = { ...meta, result_hash: sha256Value(result) } as Record<string, unknown>;
      if (cfg.captureResult) completed.result = result;
      emitEvent("mcp.tool_call.completed", { source: { component: "instrumentor", sdk: "mcp_js", operation: "callTool" }, data: { mcp: completed }, status: "success", latencyMs: Math.round(performance.now() - started), purposeId });
      return result;
    } catch (error: any) {
      emitEvent("mcp.tool_call.failed", { source: { component: "instrumentor", sdk: "mcp_js", operation: "callTool" }, data: { mcp: meta }, status: "error", latencyMs: Math.round(performance.now() - started), purposeId, error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) } });
      throw error;
    }
  } as any;
  wrapped[patched] = true;
  client.callTool = wrapped;
  return true;
}
