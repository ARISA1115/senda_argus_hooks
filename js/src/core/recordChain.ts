// hook が送る記録の通し番号と連鎖。Python の hooks と同じ式で、受け取り側が同じ式で計算し直す。
//
// 番号は記録を捉えた時点で付ける。送信の時に付けると、送れずに捨てた記録が番号を持たず、欠落が見えなくなる。
// 数え手はプロセスの中に 1 つで、全ての計装が同じ数え手を使う。
import { createHash, randomBytes } from "node:crypto";

const encoder = new TextEncoder();

// UTF-8 のバイトの順で比べる。Python の文字列の比較 (コードポイントの順) と同じ並びになる。
function compareUtf8(a: string, b: string): number {
  const x = encoder.encode(a);
  const y = encoder.encode(b);
  const n = Math.min(x.length, y.length);
  for (let i = 0; i < n; i++) {
    if (x[i] !== y[i]) return x[i] - y[i];
  }
  return x.length - y.length;
}

export const CHAIN_FIELD = "hook_chain";
export const SESSION_END_EVENT = "hook_session_end";

// 直列化は Python の json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False) に合わせる。
// キーの並びは、Python の文字列の比較 (コードポイントの順) と同じ、UTF-8 のバイトの順にする。
function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map((v) => canonical(v === undefined ? null : v)).join(",")}]`;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record)
    .filter((k) => record[k] !== undefined)
    .sort(compareUtf8);
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonical(record[k])}`).join(",")}}`;
}

export function digestOf(event: unknown): string {
  return createHash("sha256").update(canonical(event)).digest("hex");
}

export type ChainMeta = { session: string; seq: number; prev: string };

// 1 つの送り先ごとの連鎖。送り先ごとに別の数え手を持つ。同じ記録が複数の送り先へ出ても、送り先ごとに連続した番号になる。
export class RecordChain {
  private pid = process.pid;
  private session = randomBytes(16).toString("hex");
  private seq = 0;
  private prev = "";
  private ended = false;

  private newSession(): void {
    this.pid = process.pid;
    this.session = randomBytes(16).toString("hex");
    this.seq = 0;
    this.prev = "";
    this.ended = false;
  }

  // fork した子は、親の続きを書かない。別のプロセスは別のセッションにする。
  private renewIfForked(): void {
    if (process.pid !== this.pid) this.newSession();
  }

  private stampNow<T extends object>(event: T): T & { hook_chain: ChainMeta } {
    // 直列化して読み直した写しに付ける。送る本文を受け取り側が読み直した値と同じにし、要約を一致させる。
    const canon = JSON.parse(JSON.stringify(event)) as Record<string, unknown>;
    this.seq += 1;
    const meta: ChainMeta = { session: this.session, seq: this.seq, prev: this.prev };
    canon[CHAIN_FIELD] = meta;
    this.prev = digestOf(canon);
    return canon as T & { hook_chain: ChainMeta };
  }

  // 終了を通知したセッションへは、続きを書かない。収集が同じプロセスで再び始まったときは、新しいセッションにする。
  stamp<T extends object>(event: T): T & { hook_chain: ChainMeta } {
    this.renewIfForked();
    if (this.ended) this.newSession();
    return this.stampNow(event);
  }

  position(): { session: string; lastSeq: number } {
    this.renewIfForked();
    return { session: this.session, lastSeq: this.seq };
  }

  // セッションの終了の通知を、このセッションの最後の記録として番号を付けて返す。1 回だけ。
  // 記録を 1 件も付けていないセッションと、終了済みのセッションでは undefined を返す。
  stampEnd<T extends object>(build: (session: string, lastSeq: number) => T): (T & { hook_chain: ChainMeta }) | undefined {
    this.renewIfForked();
    if (this.ended || this.seq === 0) return undefined;
    const stamped = this.stampNow(build(this.session, this.seq));
    this.ended = true;
    return stamped;
  }
}
