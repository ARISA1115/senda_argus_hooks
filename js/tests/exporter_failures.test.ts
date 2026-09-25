import test from "node:test";
import assert from "node:assert/strict";
import { register } from "../src/register.js";
import { emitEvent, flush, shutdown } from "../src/runtime.js";
import { invokeWithArgus } from "../src/integrations/langgraph.js";
import type { EventRecord, Exporter } from "../src/core/types.js";

class MemoryExporter implements Exporter {
  events: EventRecord[] = [];
  flushed = 0;
  shutdowns = 0;
  emit(event: EventRecord) { this.events.push(event); }
  flush() { this.flushed += 1; }
  shutdown() { this.shutdowns += 1; }
}

class ThrowingExporter implements Exporter {
  emit(): void { throw new Error("EACCES: permission denied"); }
  flush(): void { throw new Error("flush failed"); }
  shutdown(): void { throw new Error("shutdown failed"); }
}

class RejectingExporter implements Exporter {
  emit(): Promise<void> { return Promise.reject(new Error("collector unavailable")); }
  flush(): Promise<void> { return Promise.reject(new Error("flush failed")); }
  shutdown(): Promise<void> { return Promise.reject(new Error("shutdown failed")); }
}

function openAiClient() {
  return {
    responses: { create: async () => ({ id: "r1", model: "gpt-test" }) },
    chat: { completions: { create: async () => ({ id: "c1" }) } },
    embeddings: { create: async () => ({ data: [] }) }
  };
}

async function collectUnhandledRejections(run: () => Promise<void>): Promise<unknown[]> {
  const seen: unknown[] = [];
  const listener = (reason: unknown) => { seen.push(reason); };
  const nodeProcess = (globalThis as any).process;
  nodeProcess.on("unhandledRejection", listener);
  try {
    await run();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
  } finally {
    nodeProcess.off("unhandledRejection", listener);
  }
  return seen;
}

test("a synchronously throwing exporter does not fail the instrumented call", async () => {
  const sink = new MemoryExporter();
  const client = openAiClient();
  register({ project: "throwing-exporter", exporters: [new ThrowingExporter(), sink] }, { openai: client });
  const result = await client.responses.create();
  assert.equal(result.id, "r1");
  assert.deepEqual(sink.events.map((event) => event.event_type), ["llm.request"]);
});

test("a rejecting exporter leaves no unhandled rejection", async () => {
  const sink = new MemoryExporter();
  const client = openAiClient();
  register({ project: "rejecting-exporter", exporters: [new RejectingExporter(), sink] }, { openai: client });
  const rejections = await collectUnhandledRejections(async () => {
    const result = await client.responses.create();
    assert.equal(result.id, "r1");
  });
  assert.deepEqual(rejections, []);
  assert.equal(sink.events.length, 1);
});

function nested(depth: number): Record<string, unknown> {
  const root: Record<string, unknown> = {};
  let current = root;
  for (let index = 0; index < depth; index += 1) {
    const child: Record<string, unknown> = {};
    current.child = child;
    current = child;
  }
  return root;
}

test("a deeply nested MCP result does not fail the tool call", async () => {
  const sink = new MemoryExporter();
  const deep = nested(6000);
  const client = { callTool: async (_request: unknown) => ({ structuredContent: deep }) };
  register({ project: "deep-result", exporters: [sink], captureResult: true }, { mcp: client, mcpMetadata: { serverName: "demo" } });
  const result = await client.callTool({ name: "lookup", arguments: {} });
  assert.equal(result.structuredContent, deep);
  assert.deepEqual(sink.events.map((event) => event.event_type), ["mcp.tool_call.requested", "mcp.tool_call.completed"]);
  const completed = sink.events[1];
  const mcp = completed.data.mcp as Record<string, any>;
  assert.equal(completed.security.observation_failed, undefined);
  assert.equal(mcp.tool, "lookup");
  assert.match(String(mcp.result_hash), /^[0-9a-f]{64}$/);
  assert.ok(JSON.stringify(mcp.result).includes("[MaxDepth]"));
});

test("undefined results from instrumented calls are returned unchanged", async () => {
  const sink = new MemoryExporter();
  const openai = {
    responses: { create: async () => undefined },
    chat: { completions: { create: async () => undefined } },
    embeddings: { create: async () => undefined }
  };
  const mcp = { callTool: async (_request: unknown) => undefined };
  register({ project: "undefined-results", exporters: [sink] }, { openai, mcp, mcpMetadata: { serverName: "demo" } });
  assert.equal(await openai.responses.create(), undefined);
  assert.equal(await mcp.callTool({ name: "lookup", arguments: {} }), undefined);
  const graph = { invoke: async () => undefined };
  assert.equal(await invokeWithArgus(graph, { question: "q" }), undefined);
  assert.deepEqual(
    sink.events.map((event) => event.event_type),
    ["llm.request", "mcp.tool_call.requested", "mcp.tool_call.completed", "agent.run.started", "agent.run.completed"]
  );
  assert.ok(sink.events.every((event) => event.security.observation_failed === undefined));
});

test("shared references are kept while cycles are folded", async () => {
  const sink = new MemoryExporter();
  register({ project: "shared-refs", exporters: [sink] }, {});
  const x = { value: 1 };
  const y = { value: 2 };
  const event = emitEvent("custom.event", { data: { a: x, b: x, list: [y, y] } });
  assert.deepEqual(event.data, { a: { value: 1 }, b: { value: 1 }, list: [{ value: 2 }, { value: 2 }] });
});

test("circular data is folded even when redaction is disabled", async () => {
  const lines: string[] = [];
  const jsonSink: Exporter = { emit(event: EventRecord) { lines.push(JSON.stringify(event)); } };
  register({ project: "no-redact", exporters: [jsonSink], redact: false }, {});
  const payload: Record<string, unknown> = { name: "loop" };
  payload.self = payload;
  emitEvent("custom.event", { data: payload });
  assert.equal(lines.length, 1);
  assert.equal(JSON.parse(lines[0]).data.self, "[Circular]");
});

test("circular data is folded instead of failing the event", async () => {
  const sink = new MemoryExporter();
  register({ project: "circular-data", exporters: [sink] }, {});
  const payload: Record<string, unknown> = { name: "loop" };
  payload.self = payload;
  const event = emitEvent("custom.event", { data: payload });
  assert.equal(event.data.self, "[Circular]");
  assert.equal(sink.events.length, 1);
});

test("data that cannot be read still leaves an event marked as failed observation", async () => {
  const sink = new MemoryExporter();
  register({ project: "unreadable-data", exporters: [sink] }, {});
  const data = { get secret(): string { throw new Error("getter failed"); } };
  const event = emitEvent("custom.event", { data, status: "success", source: { sdk: "custom-sdk" }, agentId: "agent-1" });
  assert.equal(event.security.observation_failed, true);
  assert.deepEqual(event.data, {});
  assert.equal(event.source.sdk, "custom-sdk");
  assert.equal(event.agent_id, "agent-1");
  assert.equal(event.status, "success");
  assert.equal(sink.events.length, 1);
});

test("flush and shutdown reach every exporter even when one of them fails", async () => {
  const sink = new MemoryExporter();
  register({ project: "failing-flush", exporters: [new ThrowingExporter(), new RejectingExporter(), sink] }, {});
  await flush();
  await shutdown();
  assert.equal(sink.flushed, 2);
  assert.equal(sink.shutdowns, 1);
});
