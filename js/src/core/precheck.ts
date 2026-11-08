import { argumentsScanFields } from "./result_scan.js";
import { deriveAgentId } from "./identity.js";
import { sha256Value } from "./hashing.js";
import { getContext } from "./context.js";
import { argusExporter, getConfig, isCollectionRunning } from "../runtime.js";

// ツールの実行の前に Argus の判定を問い合わせ、拒否と承認待ちの呼び出しを止める。既定で無効である。
// 主体を結び付けていない鍵では全ての呼び出しが最も厳しい段階で拒否になるため、導入する側が有効にする。
const PRECHECK_PATH = "/v1/hooks/precheck";
const TIMEOUT_MS = 5000;

export class ToolCallBlocked extends Error {
  constructor(readonly decision: string, readonly reason: string, readonly tool?: string) {
    super(`Argus がツールの実行を止めました decision=${decision} tool=${tool} reason=${reason}`);
    this.name = "ToolCallBlocked";
  }
}

export function precheckEnabled(): boolean {
  // 収集を止めた後に、前の宛先へ問い合わせて呼び出しを止めない。
  return Boolean(getConfig().enforcePrecheck && argusExporter() && isCollectionRunning());
}

// scanArguments に関わらず秘匿した文を送る。送らないと内容を判定できず、承認も引数で区別できない。
// 全文のダイジェストを添える。切り詰めで中央だけが違う呼び出しの承認を、別の呼び出しへ流用させない。
function toolInput(args: unknown): Record<string, unknown> {
  const fields = argumentsScanFields(args);
  const out: Record<string, unknown> = { arguments_sha256: sha256Value(args) };
  if (fields.arguments_scan) out.arguments = fields.arguments_scan;
  if (fields.arguments_scan_truncated || fields.arguments_scan_failed) out.arguments_unreadable = true;
  return out;
}

class Rejected extends Error {}

async function ask(tool: string, args: unknown, sdk: string, mcpServer?: string): Promise<{ decision?: unknown; reason?: unknown }> {
  const exporter = argusExporter();
  if (!exporter) throw new Error("no exporter");
  const cfg = getConfig();
  const body: Record<string, unknown> = {
    product: "sdk",
    agent_id: getContext().agentId ?? cfg.agentId ?? deriveAgentId(cfg.project, cfg.environment, sdk, cfg.agentHint),
    session_id: cfg.sessionId ?? cfg.runId ?? cfg.project,
    tool_name: tool,
    tool_input: toolInput(args)
  };
  if (mcpServer) body.mcp_server = mcpServer;
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json" };
  if (exporter.key) headers["X-API-Key"] = exporter.key;
  const response = await fetch(`${exporter.base}${PRECHECK_PATH}`, {
    method: "POST", headers, body: JSON.stringify(body), signal: AbortSignal.timeout(TIMEOUT_MS)
  });
  // 4xx は判定の拒否で、接続の問題ではない。失効した鍵や端末の検証の失敗で実行させない。
  if (response.status >= 400 && response.status < 500) throw new Rejected(`the check returned ${response.status}`);
  if (!response.ok) throw new Error(`status ${response.status}`);
  return (await response.json()) as { decision?: unknown; reason?: unknown };
}

export async function checkToolCall(toolName: unknown, args: unknown, sdk: string, mcpServer?: string): Promise<void> {
  if (!precheckEnabled()) return;
  const tool = String(toolName ?? "unknown");
  let verdict: { decision?: unknown; reason?: unknown };
  try {
    verdict = await ask(tool, args, sdk, mcpServer);
    if (verdict.decision !== "allow" && verdict.decision !== "deny" && verdict.decision !== "pending") throw new Error("unknown decision");
  } catch (error) {
    if (error instanceof Rejected) throw new ToolCallBlocked("rejected", error.message, tool);
    if (getConfig().precheckFailOpen ?? true) return;
    throw new ToolCallBlocked("unavailable", "the pre-execution check is unavailable", tool);
  }
  if (verdict.decision !== "allow") throw new ToolCallBlocked(String(verdict.decision), String(verdict.reason ?? ""), tool);
}
