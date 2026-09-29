// 導入の画面の接続の確認と、収集の canary。
//
// sendConnectionCheck(token) は管理の API が発行した一回限りの値を、通常の事象と同じ
// /v1/agent-runs/ingest へ 1 回送り、サーバの判定を返す。
//
// startCanary() は署名付きの canary を一定の間隔で送る。canary は収集が動いていることを示す
// 必要があるため、Argus へ送る exporter が構成され、shutdown されていない間だけ、その exporter へ
// 渡す。exporter が詰まれば canary も止まる。
//
// どちらも事象の経路を通さない。事象の経路は全ての exporter へ書き、秘匿を当てる。JSONL に
// 値が残り、秘匿が署名した項目を書き換えうるためである。どちらも例外を送出しない。
import { createHmac, randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { getContext } from "./core/context.js";
import { newEvent } from "./core/event.js";
import type { EventRecord } from "./core/types.js";
import { argusExporter, getConfig, isCollecting, observedAgentId, onAgentObserved, onShutdown } from "./runtime.js";

export const CONNECTION_CHECK_EVENT = "onboarding.connection_check";
export const CANARY_EVENT = "collector.canary";
export const CANARY_INTERVAL_DEFAULT = 60;
export const CANARY_INTERVAL_MIN = 10;
export const CANARY_INTERVAL_MAX = 3600;
const MAX_SEQ = 2 ** 53;
const FIRST_BEAT_DELAY_MS = 1000;

const encoder = new TextEncoder();

function mac(key: string, parts: string[]): string {
  // 長さは UTF-8 のバイト数で数える。サーバと Python の hooks も同じ数え方をする。
  const message = parts.map((p) => `${encoder.encode(p).length}:${p}\n`).join("");
  return createHmac("sha256", key).update(message).digest("base64url");
}

// サーバと Python の hooks が同じ計算を持つ。三者の試験に同じ入力と期待値を置く。
export function canaryMac(
  secret: string,
  args: { agentId: string; bootId: string; seq: number; intervalSec: number; generation: number },
): string {
  return mac(secret, [
    "canary-beat", args.agentId, args.bootId, String(args.seq), String(args.intervalSec), String(args.generation),
  ]);
}

export function secretGeneration(secret: string): number | undefined {
  const dot = secret.indexOf(".");
  if (dot < 0 || dot === secret.length - 1) return undefined;
  const head = secret.slice(0, dot);
  if (!/^cs[0-9]+$/.test(head)) return undefined;
  return Number(head.slice(2));
}

// 見張る対象のエージェントの識別子。明示の値、実行の文脈、設定の値、計装の事象が送った値の順に使う。
// どれも無ければ undefined を返し、呼び出し側は送らない。導入の処理が自分で識別子を導くと、計装の
// 事象と別のエージェントとして扱われる。
export function monitoredAgentId(explicit?: string): string | undefined {
  return explicit || getContext().agentId || getConfig().agentId || observedAgentId() || undefined;
}

function buildEvent(eventType: string, data: Record<string, unknown>, agentId: string): EventRecord {
  const config = getConfig();
  const event = newEvent({
    config, context: getContext(), eventType,
    source: { component: "onboarding", sdk: "senda_argus_hooks" }, data, status: "success", agentId,
  });
  if (!event.run_id) event.run_id = `run_onboarding_${randomBytes(8).toString("hex")}`;
  return event;
}

export type ConnectionCheckVerdict = { status: string; reason: string; check_id?: string | null };

export async function sendConnectionCheck(
  token: string,
  options: { endpoint?: string; apiKey?: string; agentId?: string; timeoutMs?: number } = {},
): Promise<ConnectionCheckVerdict> {
  if (typeof token !== "string" || !token.trim()) return { status: "error", reason: "empty_token" };
  const exporter = argusExporter();
  const url = options.endpoint
    ? `${options.endpoint.replace(/\/$/, "")}/v1/agent-runs/ingest`
    : exporter?.ingestUrl ?? `${(process.env.SENDA_ARGUS_ENDPOINT ?? "http://localhost:8000").replace(/\/$/, "")}/v1/agent-runs/ingest`;
  const apiKey = options.apiKey ?? exporter?.key ?? process.env.SENDA_ARGUS_API_KEY ?? "";
  const agentId = monitoredAgentId(options.agentId);
  if (!agentId) return { status: "error", reason: "no_agent" };
  const event = buildEvent(CONNECTION_CHECK_EVENT, { token: token.trim() }, agentId);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 10000);
  try {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (apiKey) headers["X-API-Key"] = apiKey;
    const response = await fetch(url, {
      method: "POST", headers, body: JSON.stringify({ events: [event] }), signal: controller.signal,
    });
    if (!response.ok) return { status: "error", reason: `http_${response.status}` };
    const body = await response.json();
    const first = Array.isArray(body?.connection_checks) ? body.connection_checks[0] : undefined;
    return first && typeof first === "object" ? first : { status: "error", reason: "no_result" };
  } catch (error) {
    return { status: "error", reason: String((error as any)?.name ?? "Error") };
  } finally {
    clearTimeout(timer);
  }
}

class Canary {
  private seq = 0;
  private readonly bootId = randomBytes(16).toString("hex");
  private timer: any;
  private first: any;
  private boundAgent: string | undefined;
  constructor(
    private readonly secret: string,
    private readonly generation: number,
    readonly intervalSec: number,
    private readonly agentId: string | undefined,
  ) {}

  start(): void {
    // 最初の送信は少し遅らせる。起動してすぐ終わるプロセスで、送信のために終了を待たせない。
    this.first = setTimeout(() => this.beat(), FIRST_BEAT_DELAY_MS);
    this.first?.unref?.();
    this.timer = setInterval(() => this.beat(), this.intervalSec * 1000);
    // 見張りのためにプロセスの終了を引き止めない。
    this.timer?.unref?.();
  }

  stop(): void {
    if (this.first) clearTimeout(this.first);
    if (this.timer) clearInterval(this.timer);
    this.first = undefined;
    this.timer = undefined;
  }

  beat(): boolean {
    const exporter = argusExporter();
    if (!isCollecting() || !exporter) return false;
    try {
      const resolved = monitoredAgentId(this.agentId);
      // 計装の事象がまだ無く識別子が分からない間は送らない。
      if (!resolved) return false;
      const probe = buildEvent(CANARY_EVENT, {}, resolved);
      const agentId = String(probe.agent_id ?? "");
      // 鍵はエージェントの識別子に束ねてある。始めた時点から識別子が変わったら送らない。新しい
      // 識別子では署名が合わず、古い識別子で送り続けると止まった収集を生きて見せる。
      this.boundAgent ??= agentId;
      if (agentId !== this.boundAgent) return false;
      const seq = this.seq;
      this.seq = (this.seq + 1) % MAX_SEQ;
      probe.data = {
        boot_id: this.bootId, seq, interval_sec: this.intervalSec, gen: this.generation,
        mac: canaryMac(this.secret, {
          agentId, bootId: this.bootId, seq, intervalSec: this.intervalSec, generation: this.generation,
        }),
      };
      exporter.emitUntracked(probe);
      return true;
    } catch {
      return false;
    }
  }
}

let canary: Canary | undefined;
let hooked = false;

export function startCanary(options: { secret?: string; intervalSec?: number; agentId?: string } = {}): boolean {
  const secret = options.secret ?? process.env.SENDA_ARGUS_CANARY_SECRET ?? "";
  const generation = secret ? secretGeneration(secret) : undefined;
  if (generation === undefined || canary) return false;
  let interval = options.intervalSec ?? Number(process.env.SENDA_ARGUS_CANARY_INTERVAL_SEC ?? CANARY_INTERVAL_DEFAULT);
  if (!Number.isFinite(interval)) interval = CANARY_INTERVAL_DEFAULT;
  interval = Math.min(Math.max(Math.trunc(interval), CANARY_INTERVAL_MIN), CANARY_INTERVAL_MAX);
  if (!hooked) { onShutdown(stopCanary); hooked = true; }
  canary = new Canary(secret, generation, interval, options.agentId);
  canary.start();
  return true;
}

export function stopCanary(): void {
  canary?.stop();
  canary = undefined;
}

export function currentCanary(): { intervalSec: number; beat(): boolean } | undefined { return canary; }

function checkMarker(token: string): string | undefined {
  try {
    const base = process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state");
    const digest = createHash("sha256").update(token).digest("hex").slice(0, 32);
    return path.join(base, "senda-argus", `connection-check-${digest}`);
  } catch {
    return undefined;
  }
}

// 印を原子的に取る。複数のプロセスが同時に起動しても、送るのは印を取った 1 つだけにする。
function claimMarker(marker: string): boolean {
  try {
    fs.mkdirSync(path.dirname(marker), { recursive: true });
    fs.closeSync(fs.openSync(marker, "wx"));
    return true;
  } catch {
    return false;
  }
}

function sendClaimed(token: string, marker: string | undefined): void {
  void sendConnectionCheck(token).then((result) => {
    if (!marker) return;
    try {
      // サーバが判定を返さなかったときは印を外し、後のプロセスが送れるようにする。
      if (result.status === "error") fs.rmSync(marker, { force: true });
      else fs.writeFileSync(marker, String(result.status), "utf8");
    } catch {
      // 印を扱えなくても、判定は変わらない。
    }
  }).catch(() => undefined);
}

// 自動導入から呼ぶ。接続の確認はホストで 1 回だけ送る。送る前に印を原子的に取り、印を取った
// プロセスだけが送る。サーバが判定を返さなかったときは印を外す。識別子がまだ分からなければ、計装の
// 事象が初めて送られたときに送る。canary は鍵が設定されていれば始める。
// SENDA_ARGUS_CANARY_AUTOSTART=false で止められる。
export function startFromEnv(): void {
  const token = (process.env.SENDA_ARGUS_CONNECTION_CHECK_TOKEN ?? "").trim();
  if (token) {
    const marker = checkMarker(token);
    const send = () => {
      if (marker && !claimMarker(marker)) return;
      sendClaimed(token, marker);
    };
    if (monitoredAgentId()) send();
    else onAgentObserved(() => send());
  }
  if (!autostartDisabled()) startCanary();
}

// 自動開始を止める指定か。Python の hooks と同じ値の集合で判定する。
export function autostartDisabled(): boolean {
  return /^(0|false|no|off|n)$/i.test(String(process.env.SENDA_ARGUS_CANARY_AUTOSTART ?? "").trim());
}
