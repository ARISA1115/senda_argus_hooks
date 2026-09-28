import type { EventRecord, Exporter } from "../core/types.js";
import { fallbackRunId } from "../core/event.js";

export class ArgusExporter implements Exporter {
  private readonly url: string;
  private readonly apiKey: string;
  private readonly runId?: string;
  private readonly timeoutMs: number;
  private readonly pending = new Set<Promise<void>>();

  get ingestUrl(): string { return this.url; }
  get key(): string { return this.apiKey; }

  constructor(endpoint = "http://localhost:8000", apiKey = "", runId?: string, timeoutMs = 10000) {
    this.url = `${endpoint.replace(/\/$/, "")}/v1/agent-runs/ingest`;
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

  private async send(event: EventRecord, untracked = false): Promise<void> {
    const controller = new AbortController();
    // 待ち合わせに入れない送信は、中断のタイマーでもプロセスの終了を引き止めない。
    const timer = setTimeout(() => controller.abort(), untracked ? Math.min(this.timeoutMs, 5000) : this.timeoutMs);
    if (untracked) timer?.unref?.();
    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (this.apiKey) headers["X-API-Key"] = this.apiKey;
      await fetch(this.url, {
        method: "POST",
        headers,
        body: JSON.stringify({ events: [event] }),
        signal: controller.signal,
      });
    } catch {
      // Fail open: observability must not break the agent.
    } finally {
      clearTimeout(timer);
    }
  }

  async flush(): Promise<void> {
    await Promise.allSettled([...this.pending]);
  }

  async shutdown(): Promise<void> { await this.flush(); }
}
