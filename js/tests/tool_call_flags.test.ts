import test from "node:test";
import assert from "node:assert/strict";
import { register } from "../src/register.js";
import { instrumentOpenAIAgents } from "../src/integrations/openai_agents.js";
import { toolResultIsError } from "../src/instrumentors/mcp.js";
import type { EventRecord, Exporter } from "../src/core/types.js";

class MemoryExporter implements Exporter {
  events: EventRecord[] = [];
  emit(event: EventRecord) { this.events.push(event); }
}

function setup() {
  const sink = new MemoryExporter();
  register({ project: "test", exporters: [sink], redact: true });
  return sink;
}

test("MCP result error flag is read only from a boolean", () => {
  assert.equal(toolResultIsError({ isError: true }), true);
  assert.equal(toolResultIsError({ isError: false }), false);
  assert.equal(toolResultIsError({ isError: "yes" }), undefined);
  assert.equal(toolResultIsError("text"), undefined);
});

test("OpenAI Agents tool spans carry the tool name and failed spans are not completions", async () => {
  const sink = setup();
  let processor: any;
  instrumentOpenAIAgents({ addTraceProcessor(p: any) { processor = p; } });
  await processor.onSpanStart({ spanData: { type: "function", name: "reset_password" }, spanId: "s1", traceId: "t1" });
  await processor.onSpanEnd({ spanData: { type: "function", name: "reset_password" }, spanId: "s1", traceId: "t1" });
  await processor.onSpanEnd({ spanData: { type: "function", name: "approve_reset" }, spanId: "s2", traceId: "t1", error: { message: "denied" } });
  await processor.onSpanEnd({ spanData: { type: "function" }, spanId: "s3", traceId: "t1" });
  assert.deepEqual(sink.events.map(e => e.event_type), ["tool_call.requested", "tool_call.completed", "tool_call.failed", "tool_call.completed"]);
  assert.deepEqual(sink.events.map(e => (e.data as any).tool?.tool_name), ["reset_password", "reset_password", "approve_reset", undefined]);
});
