import test from "node:test";
import assert from "node:assert/strict";
import { ArgusExporter } from "../src/exporters/argus.js";
import type { EventRecord } from "../src/core/types.js";

type Captured = { url: string; body: any };

// 先頭の fail 回の取り込みを失敗にし、その後は成功にする。報告の宛先の応答は reportOk で決める。
function stubFetch(fail: number, reportOk = true): Captured[] {
  const captured: Captured[] = [];
  let ingests = 0;
  (globalThis as any).fetch = async (url: string, init: any) => {
    captured.push({ url, body: JSON.parse(init.body) });
    if (url.endsWith("/v1/hooks/discards")) return { ok: reportOk, status: reportOk ? 200 : 503 };
    ingests += 1;
    if (ingests <= fail) throw new Error("connection refused");
    return { ok: true, status: 200 };
  };
  return captured;
}

// 送る前に今より先と 30 日より前を切り詰めるので、今から数える。
const BASE = Math.floor(Date.now() / 1000) - 3600;
const at = (sec: number) => new Date((BASE + sec) * 1000).toISOString();
const event = (id: string, sec: number) =>
  ({ event_id: id, event_type: "x", timestamp: at(sec) }) as unknown as EventRecord;

test("records that did not arrive are reported once after the next success", async () => {
  const captured = stubFetch(2);
  const exporter = new ArgusExporter("http://argus.example", "key");
  exporter.emit(event("a", 0));
  exporter.emit(event("b", 30));
  await exporter.flush();
  exporter.emit(event("c", 90));
  await exporter.flush();
  const reports = captured.filter((c) => c.url.endsWith("/v1/hooks/discards"));
  assert.deepEqual(
    reports.map((r) => r.body),
    [{ windows: [{ from: BASE, to: BASE + 30, count: 2 }] }],
  );
  assert.deepEqual(exporter.discardWindows, []);
});

test("a report that does not arrive is kept for the next success", async () => {
  stubFetch(1, false);
  const exporter = new ArgusExporter("http://argus.example", "key");
  exporter.emit(event("a", 0));
  await exporter.flush();
  exporter.emit(event("b", 500));
  await exporter.flush();
  assert.deepEqual(exporter.discardWindows, [{ from: BASE, to: BASE, count: 1 }]);
});

test("discard windows are bounded", () => {
  const exporter = new ArgusExporter("http://argus.example");
  for (let i = 0; i < 100; i += 1) exporter.noteDiscard(at(i * 1000));
  const windows = exporter.discardWindows;
  assert.equal(windows.length, 32);
  assert.equal(
    windows.reduce((a, w) => a + w.count, 0),
    100,
  );
});

test("only connection failures and retryable statuses count as discarded", async () => {
  (globalThis as any).fetch = async (url: string) => {
    if (url.endsWith("/v1/hooks/discards")) return { ok: true, status: 200 };
    return { ok: false, status: 401 };
  };
  const exporter = new ArgusExporter("http://argus.example", "key");
  exporter.emit(event("a", 0));
  await exporter.flush();
  assert.deepEqual(exporter.discardWindows, []);
});

test("a rejected report is not sent again", async () => {
  const captured: string[] = [];
  let ingests = 0;
  (globalThis as any).fetch = async (url: string) => {
    captured.push(url);
    if (url.endsWith("/v1/hooks/discards")) return { ok: false, status: 400 };
    ingests += 1;
    if (ingests === 1) throw new Error("connection refused");
    return { ok: true, status: 200 };
  };
  const exporter = new ArgusExporter("http://argus.example", "key");
  exporter.emit(event("a", 0));
  await exporter.flush();
  exporter.emit(event("b", 10));
  await exporter.flush();
  exporter.emit(event("c", 20));
  await exporter.flush();
  assert.equal(captured.filter((u) => u.endsWith("/v1/hooks/discards")).length, 1);
  assert.deepEqual(exporter.discardWindows, []);
});
