import test from "node:test";
import assert from "node:assert/strict";
import SendaArgus from "../src/index.js";

test("register and emit schema 0.2 event", async () => {
  SendaArgus.register({
    project: "test",
    exporter: "memory",
    instrumentFetch: false,
    instrumentXHR: false,
    instrumentWebSocket: false
  });
  await SendaArgus.emit("unit.test", {
    data: {token: "secret-value", authorization: "Bearer abcdef"}
  });
  const event = SendaArgus.getEvents().at(-1);
  assert.equal(event.schema_version, "0.2");
  assert.equal(event.project, "test");
  assert.equal(event.data.authorization, "***REDACTED***");
  assert.deepEqual(
    SendaArgus.getEvents().find((item) => item.event_type === "browser.ai.instrumented").data.scope,
    ["llm", "mcp"]
  );
  SendaArgus.unregister();
});
