import test from "node:test";
import assert from "node:assert/strict";
import { register } from "../src/register.js";
import { shutdown } from "../src/runtime.js";
import { ArgusExporter } from "../src/exporters/argus.js";
import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import {
  CANARY_EVENT, CONNECTION_CHECK_EVENT, canaryMac, currentCanary, secretGeneration,
  sendConnectionCheck, startCanary, startFromEnv, stopCanary,
} from "../src/onboarding.js";

// 計装が 1 つ入った状態にするための呼び出し先。
function mcpClient() {
  return { callTool: async () => ({}), listTools: async () => ({ tools: [] }), readResource: async () => ({}) };
}

async function settle(ms = 30): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve as any, ms));
}

type Captured = { url: string; headers: Record<string, string>; body: any };

function stubFetch(reply: any = { accepted: 1 }, status = 200): Captured[] {
  const captured: Captured[] = [];
  (globalThis as any).fetch = async (url: string, init: any) => {
    captured.push({ url, headers: init.headers, body: JSON.parse(init.body) });
    return { ok: status < 300, status, json: async () => reply };
  };
  return captured;
}

test("mac matches the server and Python vectors", () => {
  const args = { agentId: "agent-x", bootId: "00ff00ff", seq: 3, intervalSec: 60, generation: 1 };
  assert.equal(canaryMac("cs1.fixed", args), "zl3IRfLg4UC6WcViAfI9ZCfpNmclWxIWKi1f2LY6cag");
  // 長さは UTF-8 のバイト数で数える。文字列の length で数えると非 ASCII の識別子で割れる。
  assert.equal(
    canaryMac("cs1.fixed", { ...args, agentId: "エージェント-ü" }),
    "Pw-KPMi63QYD61QPVxObCMlUYZeFKoAKdkWjXptO5Qs",
  );
});

test("secret carries its generation", () => {
  assert.equal(secretGeneration("cs7.abc"), 7);
  for (const bad of ["", "abc", "csX.abc", "cs1", "cs1."]) assert.equal(secretGeneration(bad), undefined);
});

test("connection check is posted once and returns the verdict", async () => {
  const captured = stubFetch({ accepted: 1, connection_checks: [{ check_id: "chk_a", status: "verified", reason: "verified" }] });
  const result = await sendConnectionCheck("ac1.chk_a.1.n.s", { endpoint: "http://argus.test", apiKey: "k", agentId: "agent-1" });
  assert.deepEqual(result, { check_id: "chk_a", status: "verified", reason: "verified" });
  assert.equal(captured.length, 1);
  assert.equal(captured[0].url, "http://argus.test/v1/agent-runs/ingest");
  assert.equal(captured[0].headers["X-API-Key"], "k");
  const [event] = captured[0].body.events;
  assert.equal(event.event_type, CONNECTION_CHECK_EVENT);
  assert.equal(event.agent_id, "agent-1");
  assert.deepEqual(event.data, { token: "ac1.chk_a.1.n.s" });
  assert.ok(event.run_id);
});

test("connection check never throws", async () => {
  stubFetch({}, 500);
  assert.deepEqual(await sendConnectionCheck("t", { endpoint: "http://argus.test" }), { status: "error", reason: "http_500" });
  assert.equal((await sendConnectionCheck("")).reason, "empty_token");
});

test("canary beats through the Argus exporter only while collecting", async () => {
  const captured = stubFetch();
  register(
    { project: "canary", agentId: "agent-1", exporters: [new ArgusExporter("http://argus.test", "k-collector")] },
    { mcp: mcpClient() },
  );
  assert.equal(startCanary({ secret: "cs7.s-test", intervalSec: 10 }), true);
  assert.equal(startCanary({ secret: "cs7.s-test" }), false);
  const canary = currentCanary();
  assert.ok(canary);
  assert.equal(canary!.beat(), true);
  assert.equal(canary!.beat(), true);
  await settle();
  const beats = captured.map((c) => c.body.events[0]);
  assert.deepEqual(beats.map((b) => b.event_type), [CANARY_EVENT, CANARY_EVENT]);
  assert.deepEqual(beats.map((b) => b.data.seq), [0, 1]);
  assert.equal(captured[0].headers["X-API-Key"], "k-collector");
  for (const b of beats) {
    assert.equal(b.data.gen, 7);
    assert.equal(b.data.mac, canaryMac("cs7.s-test", {
      agentId: "agent-1", bootId: b.data.boot_id, seq: b.data.seq, intervalSec: 10, generation: 7,
    }));
  }
  await shutdown();
  // 収集を止めたら canary も止まる。止めないと生きている印だけが届く。
  assert.equal(currentCanary(), undefined);
  assert.equal(canary!.beat(), false);
});

test("canary without an Argus exporter sends nothing", () => {
  const captured = stubFetch();
  register({ project: "canary", exporters: [{ type: "null" }] });
  startCanary({ secret: "cs1.s", intervalSec: 10 });
  assert.equal(currentCanary()!.beat(), false);
  assert.equal(captured.length, 0);
  stopCanary();
});

test("canary interval is clamped to the server range", () => {
  register({ project: "canary", exporters: [{ type: "null" }] });
  startCanary({ secret: "cs1.s", intervalSec: 1 });
  assert.equal(currentCanary()!.intervalSec, 10);
  stopCanary();
  startCanary({ secret: "cs1.s", intervalSec: 99999 });
  assert.equal(currentCanary()!.intervalSec, 3600);
  stopCanary();
});

test("canary without active instrumentation sends nothing", () => {
  const captured = stubFetch();
  register({ project: "canary", exporters: [new ArgusExporter("http://argus.test", "k")] });
  startCanary({ secret: "cs1.s", intervalSec: 10 });
  assert.equal(currentCanary()!.beat(), false);
  assert.equal(captured.length, 0);
  stopCanary();
});

function freshStateHome(): string {
  const dir = path.join(process.cwd(), "dist-test", `state-${randomUUID()}`);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

test("startFromEnv sends the connection check once per host", async () => {
  const captured = stubFetch({ accepted: 1, connection_checks: [{ check_id: "chk_b", status: "verified", reason: "verified" }] });
  register({ project: "env", exporters: [new ArgusExporter("http://argus.test", "k")] }, { mcp: mcpClient() });
  process.env.XDG_STATE_HOME = freshStateHome();
  process.env.SENDA_ARGUS_CONNECTION_CHECK_TOKEN = "ac1.chk_b.1.n.s";
  process.env.SENDA_ARGUS_CANARY_AUTOSTART = "false";
  process.env.SENDA_ARGUS_CANARY_SECRET = "cs1.s-env";
  try {
    startFromEnv();
    await settle();
    startFromEnv();
    await settle();
    const checks = captured.filter((c) => c.body.events[0].event_type === CONNECTION_CHECK_EVENT);
    assert.equal(checks.length, 1);
    // 止める指定のあるプロセスでは canary を始めない。
    assert.equal(currentCanary(), undefined);
  } finally {
    delete process.env.SENDA_ARGUS_CONNECTION_CHECK_TOKEN;
    delete process.env.SENDA_ARGUS_CANARY_AUTOSTART;
    delete process.env.SENDA_ARGUS_CANARY_SECRET;
    stopCanary();
  }
});

test("startFromEnv starts the canary with the screen settings alone", () => {
  register({ project: "env", exporters: [{ type: "null" }] });
  stopCanary();
  process.env.SENDA_ARGUS_CANARY_SECRET = "cs2.s-env";
  process.env.SENDA_ARGUS_CANARY_INTERVAL_SEC = "30";
  try {
    startFromEnv();
    assert.equal(currentCanary()?.intervalSec, 30);
  } finally {
    delete process.env.SENDA_ARGUS_CANARY_SECRET;
    delete process.env.SENDA_ARGUS_CANARY_INTERVAL_SEC;
    stopCanary();
  }
});

test("register after shutdown restarts the canary", async () => {
  process.env.SENDA_ARGUS_CANARY_SECRET = "cs3.s-env";
  try {
    register({ project: "restart", exporters: [{ type: "null" }] });
    assert.ok(currentCanary());
    await shutdown();
    assert.equal(currentCanary(), undefined);
    register({ project: "restart", exporters: [{ type: "null" }] });
    assert.ok(currentCanary());
  } finally {
    delete process.env.SENDA_ARGUS_CANARY_SECRET;
    stopCanary();
  }
});

for (const value of ["false", " n ", "0"]) {
  test(`register does not start the canary when disabled with ${JSON.stringify(value)}`, () => {
    stopCanary();
    process.env.SENDA_ARGUS_CANARY_SECRET = "cs3.s-env";
    process.env.SENDA_ARGUS_CANARY_AUTOSTART = value;
    try {
      register({ project: "disabled", exporters: [{ type: "null" }] });
      assert.equal(currentCanary(), undefined);
    } finally {
      delete process.env.SENDA_ARGUS_CANARY_SECRET;
      delete process.env.SENDA_ARGUS_CANARY_AUTOSTART;
      stopCanary();
    }
  });
}
