import test from "node:test";
import assert from "node:assert/strict";
import { register } from "../src/register.js";
import { shutdown } from "../src/runtime.js";
import { CALL_ID_META_KEY, withCallId } from "../src/instrumentors/mcp.js";
import type { EventRecord, Exporter } from "../src/core/types.js";

class MemoryExporter implements Exporter {
  events: EventRecord[] = [];
  emit(event: EventRecord) { this.events.push(event); }
}

function recordingClient() {
  const seen: { request?: any } = {};
  const client = {
    getServerVersion: () => ({ name: "call-id-server", version: "1" }),
    callTool: async (request: any) => {
      seen.request = request;
      return { content: [{ type: "text", text: "ok" }] };
    },
  };
  return { client, seen };
}

function requested(sink: MemoryExporter): any {
  return sink.events.find(e => e.event_type === "mcp.tool_call.requested")?.data;
}

test("a call carries one id in the request meta and in the record", async () => {
  const sink = new MemoryExporter();
  const { client, seen } = recordingClient();
  register({ project: "call-id", exporters: [sink] }, { mcp: client });
  await client.callTool({ name: "lookup", arguments: { q: 1 }, _meta: { other: "kept" } });
  await shutdown();
  const sent = seen.request._meta[CALL_ID_META_KEY];
  assert.match(sent, /^call_[0-9a-f]{32}$/);
  assert.equal(seen.request._meta.other, "kept");
  assert.equal(requested(sink).mcp.call_id, sent);
  const completed = sink.events.find(e => e.event_type === "mcp.tool_call.completed");
  assert.equal((completed?.data as any).mcp.call_id, sent);
});

test("a well formed id from the caller is reused", async () => {
  const sink = new MemoryExporter();
  const { client, seen } = recordingClient();
  register({ project: "call-id", exporters: [sink] }, { mcp: client });
  await client.callTool({ name: "lookup", _meta: { [CALL_ID_META_KEY]: "call_caller_0001" } });
  await shutdown();
  assert.equal(seen.request._meta[CALL_ID_META_KEY], "call_caller_0001");
  assert.equal(requested(sink).mcp.call_id, "call_caller_0001");
});

test("a malformed id from the caller is replaced", () => {
  const { request, callId } = withCallId({ name: "t", _meta: { [CALL_ID_META_KEY]: "short" } });
  assert.match(callId as string, /^call_/);
  assert.equal(request._meta[CALL_ID_META_KEY], callId);
});

test("the argument digest does not depend on the attached id", async () => {
  const sink = new MemoryExporter();
  const { client } = recordingClient();
  register({ project: "call-id", exporters: [sink] }, { mcp: client });
  await client.callTool({ name: "lookup", arguments: { q: 1 } });
  await client.callTool({ name: "lookup", arguments: { q: 1 } });
  await shutdown();
  const hashes = sink.events.filter(e => e.event_type === "mcp.tool_call.requested").map(e => (e.data as any).mcp);
  assert.equal(hashes.length, 2);
  assert.notEqual(hashes[0].call_id, hashes[1].call_id);
  assert.equal(hashes[0].arguments_hash, hashes[1].arguments_hash);
});

test("a request that cannot carry meta is passed through untouched", () => {
  const odd = { name: "t", _meta: "not an object" };
  assert.equal(withCallId(odd).request, odd);
  assert.equal(withCallId(odd).callId, undefined);
  assert.equal(withCallId(null).request, null);
  const list: any = [];
  assert.equal(withCallId(list).request, list);
});
