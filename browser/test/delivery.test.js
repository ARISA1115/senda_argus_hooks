import test from "node:test";
import assert from "node:assert/strict";
import SendaArgus from "../src/index.js";

const ENDPOINT = "https://collector.example/v1/agent-runs/ingest";
const KEEPALIVE_LIMIT = 64 * 1024;

function override(name, value) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, name);
  Object.defineProperty(globalThis, name, {value, configurable: true, writable: true});
  return () => {
    if (previous) Object.defineProperty(globalThis, name, previous);
    else delete globalThis[name];
  };
}

// ブラウザと同じく、keepalive の本文が 64 KiB を超えたら送る前に TypeError で拒む。
function stubFetch(statuses) {
  const calls = [];
  const nativeFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (init.keepalive && new TextEncoder().encode(init.body).length > KEEPALIVE_LIMIT) {
      throw new TypeError("Failed to fetch");
    }
    calls.push({url, init});
    const status = statuses[Math.min(calls.length - 1, statuses.length - 1)];
    return new Response("{}", {status});
  };
  return {calls, restore: () => { globalThis.fetch = nativeFetch; }};
}

function stubWarn() {
  const warnings = [];
  const nativeWarn = console.warn;
  console.warn = (message) => { warnings.push(String(message)); };
  return {warnings, restore: () => { console.warn = nativeWarn; }};
}

function registerHttp(options = {}) {
  SendaArgus.unregister();
  SendaArgus.clearEvents();
  SendaArgus.register({
    project: "delivery-test",
    exporter: "http",
    endpoint: ENDPOINT,
    batchSize: 1000,
    instrumentFetch: false,
    instrumentXHR: false,
    instrumentWebSocket: false,
    ...options
  });
}

function sentEventIds(calls) {
  return calls.flatMap((call) => JSON.parse(call.init.body).events.map((item) => item.event_id));
}

test("configured headers bypass sendBeacon and reach the collector through fetch", async () => {
  const beacons = [];
  const restoreNavigator = override("navigator", {sendBeacon: (...args) => { beacons.push(args); return true; }});
  const fetchStub = stubFetch([200]);
  try {
    registerHttp({headers: {"x-api-key": "collect-key"}});
    await SendaArgus.emit("unit.test");
    await SendaArgus.flush();
    assert.equal(beacons.length, 0);
    assert.equal(fetchStub.calls.length, 1);
    assert.equal(fetchStub.calls[0].init.headers["x-api-key"], "collect-key");
  } finally {
    SendaArgus.unregister();
    fetchStub.restore();
    restoreNavigator();
  }
});

test("only headers that a collector CORS policy can allow are sent", async () => {
  const restoreNavigator = override("navigator", undefined);
  const fetchStub = stubFetch([200]);
  try {
    registerHttp({headers: {"x-api-key": "collect-key"}});
    await SendaArgus.emit("unit.test");
    await SendaArgus.flush();
    assert.deepEqual(
      Object.keys(fetchStub.calls[0].init.headers).map((name) => name.toLowerCase()).sort(),
      ["content-type", "x-api-key"]
    );
  } finally {
    SendaArgus.unregister();
    fetchStub.restore();
    restoreNavigator();
  }
});

test("sendBeacon is still used when no header has a value", async () => {
  const beacons = [];
  const restoreNavigator = override("navigator", {sendBeacon: (...args) => { beacons.push(args); return true; }});
  const fetchStub = stubFetch([200]);
  try {
    registerHttp({headers: {"x-api-key": undefined}});
    await SendaArgus.emit("unit.test");
    await SendaArgus.flush();
    assert.equal(beacons.length, 1);
    assert.equal(fetchStub.calls.length, 0);
  } finally {
    SendaArgus.unregister();
    fetchStub.restore();
    restoreNavigator();
  }
});

test("a batch refused with a retryable status is sent again", async () => {
  const restoreNavigator = override("navigator", undefined);
  const fetchStub = stubFetch([503, 200]);
  try {
    registerHttp({headers: {"x-api-key": "collect-key"}});
    const event = await SendaArgus.emit("unit.test");
    await SendaArgus.flush();
    await SendaArgus.flush({force: true});
    assert.equal(fetchStub.calls.length, 2);
    assert.ok(sentEventIds([fetchStub.calls[1]]).includes(event.event_id));
  } finally {
    SendaArgus.unregister();
    fetchStub.restore();
    restoreNavigator();
  }
});

test("events kept during an outage still reach the collector after it recovers", async () => {
  const restoreNavigator = override("navigator", undefined);
  const fetchStub = stubFetch([503, 200]);
  try {
    registerHttp({headers: {"x-api-key": "collect-key"}});
    const emitted = [];
    for (let index = 0; index < 60; index += 1) {
      emitted.push(await SendaArgus.emit("unit.test", {data: {padding: "x".repeat(1200), index}}));
    }
    await SendaArgus.flush();
    for (let index = 0; index < 20; index += 1) {
      emitted.push(await SendaArgus.emit("unit.test", {data: {padding: "x".repeat(1200), index}}));
    }
    await SendaArgus.flush({force: true});
    const delivered = new Set(sentEventIds(fetchStub.calls.slice(1)));
    for (const event of emitted) assert.ok(delivered.has(event.event_id));
    for (const call of fetchStub.calls) {
      assert.ok(new TextEncoder().encode(call.init.body).length <= KEEPALIVE_LIMIT);
    }
  } finally {
    SendaArgus.unregister();
    fetchStub.restore();
    restoreNavigator();
  }
});

test("emits during a retry wait do not send the refused batch again", async () => {
  const restoreNavigator = override("navigator", undefined);
  const fetchStub = stubFetch([401]);
  try {
    registerHttp({headers: {"x-api-key": "wrong-key"}, batchSize: 1});
    await SendaArgus.ready();
    await new Promise((resolve) => setImmediate(resolve));
    for (let index = 0; index < 30; index += 1) await SendaArgus.emit("unit.test");
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(fetchStub.calls.length, 1);
  } finally {
    SendaArgus.unregister();
    fetchStub.restore();
    restoreNavigator();
  }
});

test("a batch refused as malformed is dropped with a warning and does not block later events", async () => {
  const restoreNavigator = override("navigator", undefined);
  const fetchStub = stubFetch([400, 200]);
  const warn = stubWarn();
  try {
    registerHttp({headers: {"x-api-key": "collect-key"}});
    const refused = await SendaArgus.emit("unit.test");
    await SendaArgus.flush();
    const later = await SendaArgus.emit("unit.test");
    await SendaArgus.flush();
    const resent = sentEventIds(fetchStub.calls.slice(1));
    assert.ok(resent.includes(later.event_id));
    assert.ok(!resent.includes(refused.event_id));
    assert.ok(warn.warnings.some((message) => message.includes("collector responded 400")));
  } finally {
    SendaArgus.unregister();
    fetchStub.restore();
    warn.restore();
    restoreNavigator();
  }
});

test("agent_id is taken from the configuration at the time emit is called", async () => {
  SendaArgus.unregister();
  SendaArgus.clearEvents();
  SendaArgus.register({
    project: "context-test",
    exporter: "memory",
    agentId: "planner",
    instrumentFetch: false,
    instrumentXHR: false,
    instrumentWebSocket: false
  });
  try {
    const pending = SendaArgus.emit("agent.step");
    SendaArgus.setContext({agentId: "executor"});
    const event = await pending;
    assert.equal(event.agent_id, "planner");
  } finally {
    SendaArgus.unregister();
  }
});

test("the startup event is recorded before an event emitted right after register", async () => {
  const realCrypto = globalThis.crypto;
  let calls = 0;
  const restoreCrypto = override("crypto", {
    subtle: {
      digest: async (algorithm, data) => {
        calls += 1;
        if (calls === 1) await new Promise((resolve) => setTimeout(resolve, 50));
        return realCrypto.subtle.digest(algorithm, data);
      }
    }
  });
  try {
    SendaArgus.unregister();
    SendaArgus.clearEvents();
    SendaArgus.register({
      project: "order-test",
      exporter: "memory",
      instrumentFetch: false,
      instrumentXHR: false,
      instrumentWebSocket: false
    });
    await SendaArgus.emit("unit.test");
    assert.deepEqual(
      SendaArgus.getEvents().map((item) => item.event_type),
      ["browser.ai.instrumented", "unit.test"]
    );
    const startup = await SendaArgus.ready();
    assert.equal(startup.event_type, "browser.ai.instrumented");
  } finally {
    SendaArgus.unregister();
    restoreCrypto();
  }
});
