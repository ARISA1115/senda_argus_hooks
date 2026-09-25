import test from "node:test";
import assert from "node:assert/strict";
import SendaArgus from "../src/index.js";

function setupFetch(responseBody = {ok: true}) {
  const nativeFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify(responseBody), {
    status: 200,
    headers: {"content-type": "application/json"}
  });
  return nativeFetch;
}

test("captures OpenAI-compatible fetch without raw prompt by default", async () => {
  const nativeFetch = setupFetch({choices: [{message: {content: "ok"}}]});
  SendaArgus.clearEvents();
  SendaArgus.register({project: "fetch-test", exporter: "memory", instrumentXHR: false, instrumentWebSocket: false});

  await fetch("https://llm.example/v1/chat/completions", {
    method: "POST",
    headers: {"content-type": "application/json", authorization: "Bearer secret"},
    body: JSON.stringify({model: "test-model", messages: [{role: "user", content: "private prompt"}]})
  });

  const events = SendaArgus.getEvents();
  const request = events.find((event) => event.event_type === "llm.request");
  const response = events.find((event) => event.event_type === "llm.response");
  assert.equal(request.data.llm.model, "test-model");
  assert.equal(request.data.llm.input, undefined);
  assert.match(request.data.llm.messages_hash, /^(sha256|fnv1a):/);
  assert.equal(response.status, "success");

  SendaArgus.unregister();
  globalThis.fetch = nativeFetch;
});

test("captures MCP tools/call metadata", async () => {
  const nativeFetch = setupFetch({jsonrpc: "2.0", id: 1, result: {content: []}});
  SendaArgus.clearEvents();
  SendaArgus.register({project: "mcp-test", exporter: "memory", instrumentXHR: false, instrumentWebSocket: false});

  await fetch("https://mcp.example/mcp", {
    method: "POST",
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {name: "search", arguments: {q: "secret"}}
    })
  });

  const events = SendaArgus.getEvents();
  const request = events.find((event) => event.event_type === "mcp.tool_call.requested");
  const completed = events.find((event) => event.event_type === "mcp.tool_call.completed");
  assert.equal(request.data.mcp.tool, "search");
  assert.equal(request.data.mcp.arguments, undefined);
  assert.match(request.purpose_id, /^purpose_/);
  assert.equal(completed.status, "success");

  SendaArgus.unregister();
  globalThis.fetch = nativeFetch;
});

test("ignores ordinary REST API traffic", async () => {
  const nativeFetch = setupFetch({users: []});
  SendaArgus.clearEvents();
  SendaArgus.register({project: "noise-test", exporter: "memory", instrumentXHR: false, instrumentWebSocket: false});

  await fetch("https://app.example/api/users", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({filter: "active"})
  });

  const observed = SendaArgus.getEvents().filter((event) =>
    event.event_type.startsWith("llm.") || event.event_type.startsWith("mcp.")
  );
  assert.equal(observed.length, 0);

  SendaArgus.unregister();
  globalThis.fetch = nativeFetch;
});

test("ignores non-MCP JSON-RPC traffic", async () => {
  const nativeFetch = setupFetch({jsonrpc: "2.0", id: 1, result: "ok"});
  SendaArgus.clearEvents();
  SendaArgus.register({project: "jsonrpc-test", exporter: "memory", instrumentXHR: false, instrumentWebSocket: false});

  await fetch("https://rpc.example/jsonrpc", {
    method: "POST",
    body: JSON.stringify({jsonrpc: "2.0", id: 1, method: "eth_blockNumber", params: []})
  });

  const observed = SendaArgus.getEvents().filter((event) => event.event_type.startsWith("mcp."));
  assert.equal(observed.length, 0);

  SendaArgus.unregister();
  globalThis.fetch = nativeFetch;
});
