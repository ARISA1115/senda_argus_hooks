import test from "node:test";
import assert from "node:assert/strict";
import { register } from "../src/register.js";
import type { EventRecord, Exporter } from "../src/core/types.js";

class MemoryExporter implements Exporter {
  events: EventRecord[] = [];
  emit(event: EventRecord) { this.events.push(event); }
}

test("OpenAI hooks emit schema 0.2 events", async () => {
  const sink = new MemoryExporter();
  const client = {
    responses: { create: async (p: any) => ({ id: "r1", model: p.model, usage: { input_tokens: 1 } }) },
    chat: { completions: { create: async () => ({ id: "c1" }) } },
    embeddings: { create: async () => ({ data: [{ embedding: [0.1] }] }) }
  };
  const installed = register({ project: "test", exporters: [sink], redact: true }, { openai: client });
  assert.equal(installed.openai, true);
  await client.responses.create({ model: "gpt-test", input: "hello" });
  assert.equal(sink.events.length, 1);
  assert.equal(sink.events[0].schema_version, "0.2");
  assert.equal(sink.events[0].event_type, "llm.request");
  assert.equal(sink.events[0].source.sdk, "openai");
  assert.equal(sink.events[0].runtime.runtime, "node");
  assert.equal(sink.events[0].security.redacted, true);
});

test("MCP hook emits requested and completed", async () => {
  const sink = new MemoryExporter();
  const client = { callTool: async (req: any) => ({ content: [{ type: "text", text: req.name }] }) };
  register({ project: "test", exporters: [sink] }, { mcp: client, mcpMetadata: { serverName: "demo", serverUrl: "https://mcp.example/" } });
  await client.callTool({ name: "lookup", arguments: { q: "CVE-2024-3094" } });
  assert.deepEqual(sink.events.map(e => e.event_type), ["mcp.tool_call.requested", "mcp.tool_call.completed"]);
  assert.match(String(sink.events[0].purpose_id), /^purpose_/);
});
