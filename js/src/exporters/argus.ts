import type { EventRecord, Exporter } from "../core/types.js";
import { fallbackRunId } from "../core/event.js";

// 捨てた記録の時間帯を報告する宛先と、持つ時間帯の数と、前の時間帯へ寄せる間隔。
const DISCARD_PATH = "/v1/hooks/discards";
const MAX_DISCARD_WINDOWS = 32;
const DISCARD_MERGE_SEC = 60;
// 受け取り側が受ける時間帯の範囲。送る前に同じ範囲へ切り詰め、範囲外で報告ごと拒まれないようにする。
const DISCARD_MAX_SPAN = 7 * 86400;
const DISCARD_MAX_AGE = 30 * 86400;
// 届かなかったとして数える応答。Python の送出が送り直す状態と揃える。
const RETRY_STATUSES = new Set([502, 503, 504]);

type DiscardWindow = { from: number; to: number; count: number };

export class ArgusExporter implements Exporter {
  private readonly url: string;
  private readonly discardUrl: string;
  private readonly apiKey: string;
  private readonly runId?: string;
  private readonly timeoutMs: number;
  private readonly pending = new Set<Promise<void>>();
  // 届かなかった記録の時間帯。送信が回復した後に Argus へ報告する。受け取り側はこの時間帯の中継に
  // だけある記録を、hooks の記録の欠落ではなく取り込みの欠落として数える。
  private discards: DiscardWindow[] = [];
  private reporting = false;

  get ingestUrl(): string { return this.url; }
  get key(): string { return this.apiKey; }
  get discardWindows(): DiscardWindow[] { return this.discards.map((w) => ({ ...w })); }

  constructor(endpoint = "http://localhost:8000", apiKey = "", runId?: string, timeoutMs = 10000) {
    const base = endpoint.replace(/\/$/, "");
    this.url = `${base}/v1/agent-runs/ingest`;
    this.discardUrl = `${base}${DISCARD_PATH}`;
    this.apiKey = apiKey;
    this.runId = runId;
    this.timeoutMs = timeoutMs;
  }

  // 終了の待ち合わせに入れずに送る。canary の送信でプロセスの終了を待たせない。
  emitUntracked(event: EventRecord): void {
    void this.send(event, true);
  }

  emit(event: EventRecord): void {
    // 送り先に run を指定したときは、計装が付けたプロセスの既定の run より指定を優先する。
    const enriched = this.runId && (!event.run_id || event.run_id === fallbackRunId()) ? { ...event, run_id: this.runId } : event;
    const task = this.send(enriched).finally(() => this.pending.delete(task));
    this.pending.add(task);
  }

  private headers(): Record<string, string> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.apiKey) headers["X-API-Key"] = this.apiKey;
    return headers;
  }

  // 捨てた記録の時刻を時間帯に寄せる。数に上限を置き、超えたら古い時間帯を次へ寄せる。
  noteDiscard(timestamp: unknown): void {
    const parsed = typeof timestamp === "string" ? Date.parse(timestamp) / 1000 : Number.NaN;
    const at = Number.isFinite(parsed) ? parsed : Date.now() / 1000;
    const last = this.discards.at(-1);
    if (last && at >= last.from - DISCARD_MERGE_SEC && at <= last.to + DISCARD_MERGE_SEC) {
      last.from = Math.min(last.from, at);
      last.to = Math.max(last.to, at);
      last.count += 1;
    } else {
      this.discards.push({ from: at, to: at, count: 1 });
    }
    this.bound();
  }

  private bound(): void {
    while (this.discards.length > MAX_DISCARD_WINDOWS) {
      const oldest = this.discards.shift() as DiscardWindow;
      const head = this.discards[0];
      head.from = Math.min(head.from, oldest.from);
      head.to = Math.max(head.to, oldest.to);
      head.count += oldest.count;
    }
  }

  // 持っている時間帯を 1 回だけ報告する。届かなければ戻し、次の成功で送る。
  private static clip(windows: DiscardWindow[], now: number): DiscardWindow[] {
    const out: DiscardWindow[] = [];
    for (const w of windows) {
      const to = Math.min(w.to, now);
      const from = Math.min(Math.max(w.from, to - DISCARD_MAX_SPAN), to);
      if (from < now - DISCARD_MAX_AGE) continue;
      out.push({ from, to, count: w.count });
    }
    return out;
  }

  private async reportDiscards(): Promise<void> {
    if (this.reporting || this.discards.length === 0) return;
    this.reporting = true;
    const windows = ArgusExporter.clip(this.discards, Date.now() / 1000);
    this.discards = [];
    if (windows.length === 0) {
      this.reporting = false;
      return;
    }
    let delivered = false;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    timer?.unref?.();
    try {
      const res = await fetch(this.discardUrl, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ windows }),
        signal: controller.signal,
      });
      // 4xx は送り直しても同じ拒否になるので、届いたものとして戻さない。
      delivered = res.ok || (res.status >= 400 && res.status < 500);
    } catch {
      delivered = false;
    } finally {
      clearTimeout(timer);
      if (!delivered) {
        this.discards = [...windows, ...this.discards];
        this.bound();
      }
      this.reporting = false;
    }
  }

  private async send(event: EventRecord, untracked = false): Promise<void> {
    const controller = new AbortController();
    // 待ち合わせに入れない送信は、中断のタイマーでもプロセスの終了を引き止めない。
    const timer = setTimeout(() => controller.abort(), untracked ? Math.min(this.timeoutMs, 5000) : this.timeoutMs);
    if (untracked) timer?.unref?.();
    let delivered = false;
    let lost = false;
    try {
      const res = await fetch(this.url, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ events: [event] }),
        signal: controller.signal,
      });
      delivered = res.ok;
      lost = RETRY_STATUSES.has(res.status);
    } catch {
      // Fail open: observability must not break the agent.
      lost = true;
    } finally {
      clearTimeout(timer);
    }
    if (untracked) return;
    if (delivered) {
      await this.reportDiscards();
    } else if (lost) {
      this.noteDiscard((event as { timestamp?: unknown }).timestamp);
    }
  }

  async flush(): Promise<void> {
    await Promise.allSettled([...this.pending]);
  }

  async shutdown(): Promise<void> { await this.flush(); }
}
