import { randomUUID } from "node:crypto";
import { performance } from "node:perf_hooks";
import { sha256Value } from "../core/hashing.js";
import { newTraceId, runWithContext } from "../core/context.js";
import { dataSourceHash, deriveMcpProfileId, derivePurposeId, mcpDataSourceProfile, normalizeUrl } from "../core/identity.js";
import { emitEvent, getConfig, observe } from "../runtime.js";
import { getMcpToolDirectory, toolNamesOf, UNNAMED_MCP_SERVER } from "../core/mcp_tools.js";
import { normalizeProviderUrl, toolDefinitionHashes } from "../core/tool_definitions.js";
import { resultScanFields } from "../core/result_scan.js";

const patched = Symbol.for("senda.argus.mcp.patched");

type McpMetadata = { serverName?: string; serverUrl?: string; capability?: string };

// サーバ名の読み方は 1 つにする。呼び出しと一覧で読み方が違うと、同じサーバの一覧と呼び出しが別の
// サーバとして記録され、候補の帰属と承認したサーバが一致しなくなる。明示の名前が無いときは、SDK の
// Client が初期化で受けたサーバの名乗り (getServerVersion) を読む。
export function resolveMcpServerName(client: any, metadata: McpMetadata = {}): string {
  const candidates = [metadata.serverName, client?.serverName, client?.name];
  for (const value of candidates) if (typeof value === "string" && value) return value;
  try {
    const info = typeof client?.getServerVersion === "function" ? client.getServerVersion() : undefined;
    const announced = typeof info?.name === "string" ? info.name.trim() : "";
    // 名乗りはサーバが決める値である。別のクライアントが同じ名前を名乗っていれば使わない。
    if (announced && client && typeof client === "object" && getMcpToolDirectory().claim(announced, client)) return announced;
  } catch {
    // 名乗りが読めなくても呼び出しは止めない
  }
  return UNNAMED_MCP_SERVER;
}

function instrumentListTools(client: any, metadata: McpMetadata): boolean {
  if (!client || typeof client.listTools !== "function" || client.listTools[patched]) return false;
  const original = client.listTools;
  const wrapped = async function(this: unknown, ...args: any[]) {
    const started = performance.now();
    const result = await original.apply(this, args);
    // 一覧に出たツールをサーバごとに控える。LLM に差し出した候補のサーバはここから引く。
    observe(() => getMcpToolDirectory().record(resolveMcpServerName(client, metadata), toolNamesOf(result), client));
    // 受け取った定義のダイジェストを送る。Argus は提供元へ自分で取得した定義と突き合わせ、呼び出し元によって
    // 定義を変える提供元を捉える。本文は送らない。突き合わせの鍵はサーバの URL で、Python の計装と同じ形に揃える。
    observe(() => {
      const hashes = toolDefinitionHashes(result);
      if (Object.keys(hashes).length === 0) return;
      const serverName = resolveMcpServerName(client, metadata);
      const serverUrl = metadata.serverUrl ?? client.serverUrl ?? client.url ?? client.baseUrl ?? null;
      const mcp: Record<string, unknown> = {
        // 突き合わせの鍵は提供元の正規化で作る。URL に含まれる資格情報を送らず、既定のポートや区切りの違いで
        // Argus の取得と別の鍵にならないようにする。
        operation: "list_tools", server: serverName, server_url: normalizeProviderUrl(serverUrl),
        mcp_profile_id: deriveMcpProfileId(serverName, serverUrl), tool_definition_hashes: hashes
      };
      emitEvent("mcp.list_tools.completed", { source: { component: "instrumentor", sdk: "mcp_js", operation: "listTools" }, data: { mcp }, status: "success", latencyMs: Math.round(performance.now() - started) });
    });
    return result;
  } as any;
  wrapped[patched] = true;
  client.listTools = wrapped;
  return true;
}

// 呼び出しごとの相関の識別子。要求の _meta に載せ、中継が同じ値を自分の記録へ載せる。
// 2 つの記録を、セッションの識別子に頼らず呼び出し単位で突き合わせる鍵になる。
export const CALL_ID_META_KEY = "argus/call_id";
const CALL_ID_FORMAT = /^[A-Za-z0-9._:-]{8,128}$/;

/**
 * 要求の _meta に識別子を載せた要求と、載せた値を返す。載せられないときは要求をそのまま返す。
 * 呼び出し元が形の合う識別子を既に載せていれば、それを使う。呼び出し元の他の _meta は保つ。
 * 失敗しても本来の呼び出しは止めない。
 */
export function withCallId(request: any): { request: any; callId?: string } {
  try {
    if (request === null || typeof request !== "object" || Array.isArray(request)) return { request };
    const held = request._meta;
    if (held !== undefined && (held === null || typeof held !== "object" || Array.isArray(held))) return { request };
    const existing = held?.[CALL_ID_META_KEY];
    if (typeof existing === "string" && CALL_ID_FORMAT.test(existing)) return { request, callId: existing };
    const callId = `call_${randomUUID().replace(/-/g, "")}`;
    return { request: { ...request, _meta: { ...(held ?? {}), [CALL_ID_META_KEY]: callId } }, callId };
  } catch {
    return { request };
  }
}

export function instrumentMCP(client: any, metadata: McpMetadata = {}): boolean {
  const listed = instrumentListTools(client, metadata);
  if (!client || typeof client.callTool !== "function" || client.callTool[patched]) return listed;
  const original = client.callTool;
  const wrapped = async function(this: unknown, request: any, ...rest: any[]) {
    const traceId = newTraceId();
    return runWithContext({ traceId }, async () => {
      const started = performance.now();
      const cfg = getConfig();
      const toolName = request?.name ?? "unknown";
      const serverName = resolveMcpServerName(client, metadata);
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
      // 識別子は引数のダイジェストと記録した引数を作った後に載せる。載せる前の要求で作るので、
      // 同じ呼び出しのダイジェストが識別子の値で変わらない。
      const attached = withCallId(request);
      if (attached.callId) meta.call_id = attached.callId;
      emitEvent("mcp.tool_call.requested", { source: { component: "instrumentor", sdk: "mcp_js", operation: "callTool" }, data: { mcp: meta }, status: "start", purposeId });
      let result: any;
      try {
        result = await original.call(this, attached.request, ...rest);
      } catch (error: any) {
        observe(() => emitEvent("mcp.tool_call.failed", { source: { component: "instrumentor", sdk: "mcp_js", operation: "callTool" }, data: { mcp: meta }, status: "error", latencyMs: Math.round(performance.now() - started), purposeId, error: { type: error?.constructor?.name ?? "Error", message: String(error?.message ?? error) } }));
        throw error;
      }
      observe(() => {
        const completed = { ...meta, result_hash: sha256Value(result) } as Record<string, unknown>;
        if (cfg.captureResult) completed.result = result;
        // 結果の本文を送らない設定でも、エラーの印だけは常に送る。判別できない形なら載せない。
        const isError = toolResultIsError(result);
        if (isError !== undefined) completed.is_error = isError;
        // 本文を送らない既定でも、戻り値に埋め込まれた指示が注入の規則に届くようにする。
        if (cfg.scanResult) {
          Object.assign(completed, resultScanFields(result));
        }
        emitEvent("mcp.tool_call.completed", { source: { component: "instrumentor", sdk: "mcp_js", operation: "callTool" }, data: { mcp: completed }, status: "success", latencyMs: Math.round(performance.now() - started), purposeId });
      });
      return result;
    });
  } as any;
  wrapped[patched] = true;
  client.callTool = wrapped;
  return true;
}

/** 結果のエラーの印を返す。判別できない形なら undefined を返し、成功へ倒さない。 */
export function toolResultIsError(result: unknown): boolean | undefined {
  if (result === null || typeof result !== "object") return undefined;
  for (const key of ["isError", "is_error"]) {
    const value = (result as Record<string, unknown>)[key];
    if (typeof value === "boolean") return value;
  }
  return undefined;
}
