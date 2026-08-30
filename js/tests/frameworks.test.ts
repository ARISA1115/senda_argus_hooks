import test from "node:test";
import assert from "node:assert/strict";
import { register } from "../src/register.js";
import { langChainCallbackHandler } from "../src/integrations/langchain.js";
import { instrumentLangGraph } from "../src/integrations/langgraph.js";
import { instrumentLlamaIndex } from "../src/integrations/llamaindex.js";
import { sendaArgusLanguageModelMiddleware } from "../src/integrations/vercel_ai.js";
import { instrumentOpenAIAgents } from "../src/integrations/openai_agents.js";
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

test("LangChain callback handler emits llm and tool lifecycle events", async () => {
  const sink = setup();
  const h = langChainCallbackHandler();
  await h.handleLLMStart({ name: "fake-model" }, ["hello"], "r1");
  await h.handleLLMEnd({ generations: [[{ text: "ok" }]] }, "r1");
  await h.handleToolStart({ name: "lookup" }, { q: "CVE" }, "r2");
  await h.handleToolEnd({ ok: true }, "r2");
  assert.deepEqual(sink.events.map(e => e.event_type), ["llm.request.started", "llm.request", "tool_call.requested", "tool_call.completed"]);
});

test("LangGraph invoke wrapper emits run lifecycle events", async () => {
  const sink = setup();
  const graph: any = { invoke: async (input: any) => ({ ...input, done: true }) };
  assert.equal(instrumentLangGraph(graph), true);
  const result = await graph.invoke({ x: 1 });
  assert.equal(result.done, true);
  assert.deepEqual(sink.events.map(e => e.event_type), ["agent.run.started", "agent.run.completed"]);
});

test("LangGraph stream wrapper emits step events", async () => {
  const sink = setup();
  const graph: any = { stream: async function* () { yield { a: 1 }; yield { b: 2 }; } };
  instrumentLangGraph(graph);
  const values = [];
  for await (const chunk of graph.stream({ x: 1 })) values.push(chunk);
  assert.equal(values.length, 2);
  assert.deepEqual(sink.events.map(e => e.event_type), ["agent.run.started", "agent.step.completed", "agent.step.completed", "agent.run.completed"]);
});

test("LlamaIndex instrumentation emits retrieval embedding and query events", async () => {
  const sink = setup();
  const retriever = { retrieve: async () => [{ id: "doc1" }] };
  const embedModel = { getTextEmbedding: async () => [0.1, 0.2] };
  const queryEngine = { query: async () => ({ response: "ok" }) };
  assert.equal(instrumentLlamaIndex({ retriever, embedModel, queryEngine }), true);
  await (retriever as any).retrieve("CVE");
  await (embedModel as any).getTextEmbedding("CVE");
  await (queryEngine as any).query("CVE");
  assert.deepEqual(sink.events.map(e => e.event_type), ["retrieval.requested", "retrieval.completed", "embedding.requested", "embedding.completed", "rag.query.started", "rag.query.completed"]);
});

test("Vercel AI SDK v3 middleware emits generate event", async () => {
  const sink = setup();
  const mw = sendaArgusLanguageModelMiddleware();
  assert.equal(mw.specificationVersion, "v3");
  const out = await mw.wrapGenerate({ doGenerate: async () => ({ text: "hello" }), params: { prompt: "hi" }, model: { provider: "openai", modelId: "gpt-test" } });
  assert.equal(out.text, "hello");
  assert.equal(sink.events[0].event_type, "llm.request");
  assert.equal(sink.events[0].source.framework, "vercel-ai-sdk");
});

test("OpenAI Agents tracing processor emits normalized events", async () => {
  const sink = setup();
  let processor: any;
  const sdk = { addTraceProcessor(p: any) { processor = p; } };
  assert.equal(instrumentOpenAIAgents(sdk), true);
  await processor.onSpanStart({ spanData: { type: "function" }, spanId: "s1", traceId: "t1" });
  await processor.onSpanEnd({ spanData: { type: "function" }, spanId: "s1", traceId: "t1" });
  assert.deepEqual(sink.events.map(e => e.event_type), ["tool_call.requested", "tool_call.completed"]);
});
