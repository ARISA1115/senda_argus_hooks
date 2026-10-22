import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { CHAIN_FIELD, RecordChain, digestOf } from "../src/core/recordChain.js";
import { ArgusExporter } from "../src/exporters/argus.js";
import { canaryMac } from "../src/onboarding.js";
import { register } from "../src/register.js";
import { emitEvent, shutdown } from "../src/runtime.js";

// 受け取り側が書く式を、実装を使わずに、契約の文面どおりに書く。Python の hooks と同じ入力で同じ値になる。
function independentDigest(event: Record<string, unknown>): string {
  const walk = (v: unknown): string => {
    if (v === null || typeof v !== "object") return JSON.stringify(v);
    if (Array.isArray(v)) return `[${v.map(walk).join(",")}]`;
    const o = v as Record<string, unknown>;
    return `{${Object.keys(o).sort().map((k) => `${JSON.stringify(k)}:${walk(o[k])}`).join(",")}}`;
  };
  return createHash("sha256").update(walk(event)).digest("hex");
}

test("stamp numbers from one and links each record to the digest of the previous", () => {
  const c = new RecordChain();
  const out = [{ event_id: "a", data: { n: 1 } }, { event_id: "b" }, { event_id: "c" }].map((e) => c.stamp(e));
  assert.deepEqual(out.map((e) => e[CHAIN_FIELD].seq), [1, 2, 3]);
  assert.equal(out[0][CHAIN_FIELD].prev, "");
  assert.equal(out[1][CHAIN_FIELD].prev, independentDigest(out[0] as Record<string, unknown>));
  assert.equal(out[2][CHAIN_FIELD].prev, independentDigest(out[1] as Record<string, unknown>));
  assert.equal(out[0][CHAIN_FIELD].session.length, 32);
});

test("the digest sorts keys by code point and keeps non-ASCII characters", () => {
  const event = { b: 1, a: { z: "利用者", y: [1, 2] }, "あ": 1 };
  assert.equal(digestOf(event), independentDigest(event));
});

test("a rewritten record breaks the chain", () => {
  const c = new RecordChain();
  const first = c.stamp({ event_id: "a", data: { x: 1 } });
  const second = c.stamp({ event_id: "b" });
  const tampered = JSON.parse(JSON.stringify(first));
  tampered.data.x = 2;
  assert.notEqual(independentDigest(tampered), second[CHAIN_FIELD].prev);
});

test("the end is the last record of the session and answers once", () => {
  const c = new RecordChain();
  c.stamp({ event_id: "a" });
  c.stamp({ event_id: "b" });
  const end = c.stampEnd((session, lastSeq) => ({ event_type: "hook_session_end", session, data: { last_seq: lastSeq } }));
  assert.ok(end);
  assert.equal(end[CHAIN_FIELD].seq, 3);
  assert.equal(end.data.last_seq, 2);
  assert.equal(c.stampEnd(() => ({})), undefined);
});

test("a chain without records has no end", () => {
  assert.equal(new RecordChain().stampEnd(() => ({})), undefined);
});

test("records after the end start a new session", () => {
  const c = new RecordChain();
  const first = c.stamp({ event_id: "a" })[CHAIN_FIELD];
  c.stampEnd(() => ({ event_type: "hook_session_end" }));
  const again = c.stamp({ event_id: "b" })[CHAIN_FIELD];
  assert.notEqual(again.session, first.session);
  assert.equal(again.seq, 1);
  assert.equal(again.prev, "");
});

test("each destination has its own chain", () => {
  const one = new ArgusExporter("http://127.0.0.1:1");
  const two = new ArgusExporter("http://127.0.0.1:1");
  (globalThis as any).fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
  one.emit({ event_id: "x", event_type: "t" } as any);
  one.emit({ event_id: "y", event_type: "t" } as any);
  two.emit({ event_id: "x", event_type: "t" } as any);
  assert.equal(one.chainPosition().lastSeq, 2);
  assert.equal(two.chainPosition().lastSeq, 1);
  assert.notEqual(one.chainPosition().session, two.chainPosition().session);
});

test("a dropped record keeps its number and is declared as a range", async () => {
  const sent: any[] = [];
  (globalThis as any).fetch = async (url: string, init: any) => {
    sent.push({ url, body: JSON.parse(init.body) });
    return { ok: !String(url).includes("/ingest"), status: String(url).includes("/ingest") ? 503 : 200, json: async () => ({}) };
  };
  const exp = new ArgusExporter("http://127.0.0.1:1");
  exp.emit({ event_id: "lost", event_type: "t", timestamp: new Date().toISOString() } as any);
  await exp.flush();
  (globalThis as any).fetch = async (url: string, init: any) => {
    sent.push({ url, body: JSON.parse(init.body) });
    return { ok: true, status: 200, json: async () => ({}) };
  };
  exp.emit({ event_id: "ok", event_type: "t", timestamp: new Date().toISOString() } as any);
  await exp.flush();
  const report = sent.find((s) => String(s.url).endsWith("/v1/hooks/discards"));
  assert.ok(report);
  assert.equal(report.body.ranges.length, 1);
  assert.equal(report.body.ranges[0].from, 1);
  assert.equal(report.body.ranges[0].to, 1);
});

test("shutdown sends one session end after the records and not twice", async () => {
  const bodies: any[] = [];
  (globalThis as any).fetch = async (_url: string, init: any) => {
    bodies.push(JSON.parse(init.body));
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const exp = new ArgusExporter("http://127.0.0.1:1");
  exp.sessionEnd = (session, lastSeq) =>
    ({ event_id: `evt_${session}_end`, event_type: "hook_session_end", data: { last_seq: lastSeq } }) as any;
  exp.emit({ event_id: "a", event_type: "t" } as any);
  await exp.shutdown();
  await exp.shutdown();
  const ends = bodies.flatMap((b) => b.events ?? []).filter((e: any) => e.event_type === "hook_session_end");
  assert.equal(ends.length, 1);
  assert.equal(ends[0].data.last_seq, 1);
  assert.equal(ends[0][CHAIN_FIELD].seq, 2);
});

test("the canary mac covers the chain position", () => {
  const args = { agentId: "agent-x", bootId: "00ff00ff", seq: 3, intervalSec: 60, generation: 1 };
  const plain = canaryMac("cs1.fixed", args);
  const chained = canaryMac("cs1.fixed", { ...args, chainSession: "s".repeat(32), chainLastSeq: 5 });
  assert.notEqual(plain, chained);
  assert.notEqual(chained, canaryMac("cs1.fixed", { ...args, chainSession: "s".repeat(32), chainLastSeq: 6 }));
});

test("the session end is built with the full event schema through the runtime", async () => {
  const bodies: any[] = [];
  (globalThis as any).fetch = async (_url: string, init: any) => {
    bodies.push(JSON.parse(init.body));
    return { ok: true, status: 200, json: async () => ({}) };
  };
  register({ project: "chain-end", exporters: [{ type: "argus", endpoint: "http://127.0.0.1:1" }] }, {});
  emitEvent("probe");
  await shutdown();
  const end = bodies.flatMap((b) => b.events ?? []).find((e: any) => e.event_type === "hook_session_end");
  assert.ok(end);
  for (const field of ["schema_version", "event_id", "trace_id", "span_id", "project", "environment", "actor", "security", "runtime", "run_id", "agent_id"]) {
    assert.ok(field in end, `${field} is missing`);
  }
  assert.equal(end.data.last_seq, 1);
  assert.equal(end[CHAIN_FIELD].seq, 2);
  assert.match(end.run_id, /^(run_session_end_|run_)/);
});

test("a canary that did not arrive is declared as a discarded range", async () => {
  let ingests = 0;
  const bodies: any[] = [];
  (globalThis as any).fetch = async (url: string, init: any) => {
    bodies.push({ url, body: JSON.parse(init.body) });
    if (url.endsWith("/v1/hooks/discards")) return { ok: true, status: 200 };
    ingests += 1;
    if (ingests === 1) throw new Error("connection refused");
    return { ok: true, status: 200 };
  };
  const exp = new ArgusExporter("http://argus.example", "key");
  exp.emitUntracked({ event_id: "canary", event_type: "canary", timestamp: new Date().toISOString() } as any);
  await new Promise((resolve) => setTimeout(resolve, 20));
  exp.emit({ event_id: "next", event_type: "t", timestamp: new Date().toISOString() } as any);
  await exp.flush();
  const report = bodies.find((b) => String(b.url).endsWith("/v1/hooks/discards"));
  assert.ok(report);
  assert.deepEqual(report.body.ranges.map((r: any) => [r.from, r.to]), [[1, 1]]);
});

test("numbers are written the way the Python verifier writes them", () => {
  const sha = (text: string) => createHash("sha256").update(text).digest("hex");
  const cases: Array<[number, string]> = [
    [1e-7, "1e-07"],
    [0.00001, "1e-05"],
    [0.0001, "0.0001"],
    [0.1, "0.1"],
    [1.2345e-5, "1.2345e-05"],
    [-2.5e-7, "-2.5e-07"],
    [123.456, "123.456"],
    [1e21, "1e+21"],
    [1e20, "100000000000000000000"],
    [100, "100"],
    [-0, "0"],
  ];
  for (const [value, text] of cases) {
    assert.equal(digestOf({ n: value }), sha(`{"n":${text}}`), `${value}`);
  }
});
