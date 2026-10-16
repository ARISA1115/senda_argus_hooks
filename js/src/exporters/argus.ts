import type { EventRecord, Exporter } from "../core/types.js";
import { fallbackRunId } from "../core/event.js";

// 捨てた記録の時間帯を報告する宛先と、持つ時間帯の数と、前の時間帯へ寄せる間隔。
const DISCARD_PATH = "/v1/hooks/discards";
const MAX_DISCARD_WINDOWS = 32;
const DISCARD_MERGE_SEC = 60;
// 受け取り側が受ける時間帯の範囲。送る前に同じ範囲へ切り詰め、範囲外で報告ごと拒まれないようにする。
const DISCARD_MAX_SPAN = 7 * 86400;
const DISCARD_MAX_AGE = 30 * 86400;
// 受け取り側が 1 つの時間帯に受ける件数の上限。超えた時間帯は、他が受かる報告でも黙って捨てられる。
const DISCARD_MAX_COUNT = 100_000;

type DiscardWindow = { from: number; to: number; count: number };

// 時間帯を開始の順を保って足す。前後 DISCARD_MERGE_SEC の内で重なる時間帯は 1 つにまとめる。末尾へ足す
// だけだと、遅れた記録や時計の補正で古い範囲が来たとき並びが時刻の順でなくなり、上限での退避が離れた 2 つを寄せる。
function addWindow(windows: DiscardWindow[], from: number, to: number, count: number): DiscardWindow[] {
  const kept: DiscardWindow[] = [];
  let lo = from;
  let hi = to;
  let n = count;
  for (const w of windows) {
    if (lo <= w.to + DISCARD_MERGE_SEC && hi >= w.from - DISCARD_MERGE_SEC) {
      lo = Math.min(lo, w.from);
      hi = Math.max(hi, w.to);
      n += w.count;
    } else {
      kept.push(w);
    }
  }
  kept.push({ from: lo, to: hi, count: n });
  return kept.sort((a, b) => a.from - b.from);
}

// 数が上限を超えたら、時刻の間隔が最も狭い隣り合う 2 つを 1 つにまとめる。件数は失わない。
function boundWindows(windows: DiscardWindow[]): DiscardWindow[] {
  while (windows.length > MAX_DISCARD_WINDOWS) {
    let best = 0;
    for (let i = 1; i < windows.length - 1; i += 1) {
      if (windows[i + 1].from - windows[i].to < windows[best + 1].from - windows[best].to) best = i;
    }
    const a = windows[best];
    const b = windows[best + 1];
    windows.splice(best, 2, { from: Math.min(a.from, b.from), to: Math.max(a.to, b.to), count: a.count + b.count });
  }
  return windows;
}

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
    this.discards = boundWindows(addWindow(this.discards, at, at, 1));
  }

  // 持っている時間帯を 1 回だけ報告する。届かなければ戻し、次の成功で送る。
  private static clip(windows: DiscardWindow[], now: number): DiscardWindow[] {
    const out: DiscardWindow[] = [];
    for (const w of windows) {
      const to = Math.min(w.to, now);
      const from = Math.min(Math.max(w.from, to - DISCARD_MAX_SPAN), to);
      if (from < now - DISCARD_MAX_AGE) continue;
      // 件数が上限を超えると、受け取り側は時間帯ごと捨てる。上限へ切り詰めて時間帯を残す。
      out.push({ from, to, count: Math.min(Math.max(w.count, 1), DISCARD_MAX_COUNT) });
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
        let merged = this.discards;
        for (const w of windows) merged = addWindow(merged, w.from, w.to, w.count);
        this.discards = boundWindows(merged);
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
    try {
      const res = await fetch(this.url, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({ events: [event] }),
        signal: controller.signal,
      });
      delivered = res.ok;
    } catch {
      // Fail open: observability must not break the agent.
    } finally {
      clearTimeout(timer);
    }
    if (untracked) return;
    if (delivered) {
      await this.reportDiscards();
    } else {
      // 接続の失敗と、401、403、429 など拒まれた送信。どちらも届かなかった記録として数え、取り込みが
      // 回復した後に時間帯を報告する。数えないと、受け取り側がその間の中継にだけある記録を hooks の欠落として警報にする。
      this.noteDiscard((event as { timestamp?: unknown }).timestamp);
    }
  }

  async flush(): Promise<void> {
    await Promise.allSettled([...this.pending]);
  }

  async shutdown(): Promise<void> { await this.flush(); }
}
