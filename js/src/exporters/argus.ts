import type { EventRecord, Exporter } from "../core/types.js";

export class ArgusExporter implements Exporter {
  private readonly url: string;
  private readonly apiKey: string;
  private readonly runId?: string;
  private readonly timeoutMs: number;
  private readonly pending = new Set<Promise<void>>();

  constructor(endpoint = "http://localhost:8000", apiKey = "", runId?: string, timeoutMs = 10000) {
    this.url = `${endpoint.replace(/\/$/, "")}/v1/agent-runs/ingest`;
    this.apiKey = apiKey;
    this.runId = runId;
    this.timeoutMs = timeoutMs;
  }

  emit(event: EventRecord): void {
    const enriched = this.runId && !event.run_id ? { ...event, run_id: this.runId } : event;
    const task = this.send(enriched).finally(() => this.pending.delete(task));
    this.pending.add(task);
  }

  private async send(event: EventRecord): Promise<void> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
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
