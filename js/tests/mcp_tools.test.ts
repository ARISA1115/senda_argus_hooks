import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { register } from "../src/register.js";
import { shutdown } from "../src/runtime.js";
import { ArgusExporter } from "../src/exporters/argus.js";
import { fallbackRunId } from "../src/core/event.js";
import { McpToolDirectory, foldName, getMcpToolDirectory, offeredAlternatives, toolNamesOf } from "../src/core/mcp_tools.js";
import { resolveMcpServerName } from "../src/instrumentors/mcp.js";
import type { EventRecord, Exporter } from "../src/core/types.js";

// LLM に差し出した候補へ、MCP の一覧から引いたサーバを付けて送ることを固定する。規則は Python の実装と
// 同じで、両方の試験が同じ例 (tests/fixtures/mcp_tool_attribution.json) を読む。

class MemoryExporter implements Exporter {
  events: EventRecord[] = [];
  emit(event: EventRecord) { this.events.push(event); }
}

type Fixture = {
  cases: Array<{ name: string; servers: Array<[string, string[]]>; lookups: Record<string, string> }>;
  fold: Array<{ input: string; expected: string }>;
};
const fixture: Fixture = JSON.parse(readFileSync(join(process.cwd(), "tests", "fixtures", "mcp_tool_attribution.json"), "utf8"));

const REF = "drill-ref-a1";
const DECOY = "drill-decoy-b2";
const REF_TOOL = `${REF}_lookup_order`;
const DECOY_TOOL = `${DECOY}_lookup_order`;

for (const c of fixture.cases) {
  test(`attribution: ${c.name}`, () => {
    const directory = new McpToolDirectory();
    for (const [server, tools] of c.servers) directory.record(server, tools);
    for (const [name, expected] of Object.entries(c.lookups)) assert.equal(directory.serverOf(name), expected, name);
  });
}

test("fold matches the shared examples", () => {
  for (const v of fixture.fold) assert.equal(foldName(v.input), v.expected);
});

test("an unattributed candidate carries no server key", () => {
  getMcpToolDirectory().clear();
  getMcpToolDirectory().record("alpha", ["search"]);
  assert.deepEqual(offeredAlternatives(["search", "unlisted"]), [{ name: "search", mcp_server: "alpha" }, { name: "unlisted" }]);
  getMcpToolDirectory().clear();
});

test("the oldest server is dropped beyond the server limit and the tool limit holds", () => {
  const directory = new McpToolDirectory(2, 2);
  directory.record("s1", ["t1"]);
  directory.record("s2", ["t2"]);
  directory.record("s3", ["t3", "t4", "t5"]);
  assert.equal(directory.serverOf("t1"), "");
  assert.equal(directory.serverOf("t2"), "s2");
  assert.equal(directory.serverOf("t4"), "s3");
  assert.equal(directory.serverOf("t5"), "");
});

test("tool names are read from the SDK listing and a bare list", () => {
  assert.deepEqual(toolNamesOf({ tools: [{ name: "a" }, { name: "b" }] }), ["a", "b"]);
  assert.deepEqual(toolNamesOf([{ name: "a" }]), ["a"]);
  assert.deepEqual(toolNamesOf(undefined), []);
});

test("the server name falls back to the name the server announced", () => {
  assert.equal(resolveMcpServerName({ getServerVersion: () => ({ name: "announced" }) }), "announced");
  assert.equal(resolveMcpServerName({ serverName: "explicit", getServerVersion: () => ({ name: "announced" }) }), "explicit");
  assert.equal(resolveMcpServerName({}, { serverName: "meta" }), "meta");
  assert.equal(resolveMcpServerName({ getServerVersion: () => undefined }), "unknown");
});

function mcpClient(announced: string, tools: string[]) {
  return {
    getServerVersion: () => ({ name: announced, version: "1" }),
    listTools: async () => ({ tools: tools.map(name => ({ name })) }),
    callTool: async (req: any) => ({ content: [{ type: "text", text: req.name }] }),
  };
}

function openaiClient(selected: string) {
  return {
    responses: { create: async (_p?: any) => ({ id: "r1", output: [{ type: "function_call", name: selected }] }) },
    chat: { completions: { create: async (p: any) => ({ id: "c1", model: p.model, choices: [{ message: { tool_calls: [{ function: { name: selected, arguments: "{}" } }] } }] }) } },
    embeddings: { create: async () => ({ data: [] }) },
  };
}

test("the decision carries the server of each candidate", async () => {
  getMcpToolDirectory().clear();
  const sink = new MemoryExporter();
  const ref = mcpClient(REF, [REF_TOOL]);
  const decoy = mcpClient(DECOY, [DECOY_TOOL]);
  const llm = openaiClient(DECOY_TOOL);
  register({ project: "offered", exporters: [sink] }, { openai: llm, mcp: ref });
  register({ project: "offered", exporters: [sink] }, { mcp: decoy });
  await ref.listTools();
  await decoy.listTools();
  await llm.chat.completions.create({ model: "gpt-test", messages: [], tools: [
    { type: "function", function: { name: REF_TOOL } },
    { type: "function", function: { name: DECOY_TOOL } },
  ] });
  await decoy.callTool({ name: DECOY_TOOL, arguments: {} });
  const decisions = sink.events.filter(e => e.event_type === "agent.decision");
  assert.equal(decisions.length, 1);
  assert.equal((decisions[0].data as any).selected_tool, DECOY_TOOL);
  assert.deepEqual((decisions[0].data as any).alternatives, [
    { name: REF_TOOL, mcp_server: REF },
    { name: DECOY_TOOL, mcp_server: DECOY },
  ]);
  const completed = sink.events.filter(e => e.event_type === "mcp.tool_call.completed");
  assert.equal((completed[0].data as any).mcp.server, DECOY);
  // run の範囲を張らない受動計装でも run_id が付く。Argus は run_id の無い記録を評価しない。
  for (const e of sink.events) assert.ok(e.run_id);
  await shutdown();
  getMcpToolDirectory().clear();
});

test("responses and Anthropic shapes carry offered and selected tools", async () => {
  getMcpToolDirectory().clear();
  const sink = new MemoryExporter();
  const llm = openaiClient("pick");
  const anthropic = { messages: { create: async (_p: any) => ({ content: [{ type: "text", text: "x" }, { type: "tool_use", name: "pick" }] }) } };
  register({ project: "shapes", exporters: [sink] }, { openai: llm, anthropic });
  await llm.responses.create({ model: "m", input: "hi", tools: [{ type: "function", name: "pick" }, { type: "function", name: "other" }] });
  await anthropic.messages.create({ model: "m", messages: [], tools: [{ name: "pick" }] });
  const decisions = sink.events.filter(e => e.event_type === "agent.decision");
  assert.deepEqual(decisions.map(d => (d.data as any).alternatives), [[{ name: "pick" }, { name: "other" }], [{ name: "pick" }]]);
  await shutdown();
});

test("no decision is sent without offered tools", async () => {
  const sink = new MemoryExporter();
  const llm = openaiClient("pick");
  register({ project: "none", exporters: [sink] }, { openai: llm });
  await llm.chat.completions.create({ model: "m", messages: [] });
  assert.deepEqual(sink.events.map(e => e.event_type), ["llm.request"]);
  await shutdown();
});

test("an explicit run of the Argus exporter wins over the process fallback", () => {
  const exporter = new ArgusExporter("http://127.0.0.1:9", "", "run_explicit", 10);
  const sent: any[] = [];
  (exporter as any).send = async (event: any) => { sent.push(event); };
  exporter.emit({ run_id: fallbackRunId() } as any);
  exporter.emit({ run_id: "run_from_context" } as any);
  assert.deepEqual(sent.map(e => e.run_id), ["run_explicit", "run_from_context"]);
});

test("two clients announcing the same name get no attribution", async () => {
  getMcpToolDirectory().clear();
  const sink = new MemoryExporter();
  const ref = mcpClient(REF, [REF_TOOL]);
  const impostor = mcpClient(REF, [DECOY_TOOL]);
  const llm = openaiClient(DECOY_TOOL);
  register({ project: "impostor", exporters: [sink] }, { openai: llm, mcp: ref });
  register({ project: "impostor", exporters: [sink] }, { mcp: impostor });
  await ref.listTools();
  await impostor.listTools();
  await llm.chat.completions.create({ model: "m", messages: [], tools: [
    { type: "function", function: { name: REF_TOOL } },
    { type: "function", function: { name: DECOY_TOOL } },
  ] });
  await impostor.callTool({ name: DECOY_TOOL, arguments: {} });
  const decision = sink.events.find(e => e.event_type === "agent.decision");
  assert.deepEqual((decision?.data as any).alternatives, [{ name: REF_TOOL }, { name: DECOY_TOOL }]);
  const completed = sink.events.find(e => e.event_type === "mcp.tool_call.completed");
  assert.equal((completed?.data as any).mcp.server, "unknown");
  await shutdown();
  getMcpToolDirectory().clear();
});

test("an impostor using an explicit name conflicts and a dead owner leaves no tools", () => {
  const directory = new McpToolDirectory();
  const real = {}, impostor = {};
  directory.record("github", ["search"], real);
  assert.equal(directory.claim("github", impostor), false);
  directory.record("github", ["evil_search"], impostor);
  assert.equal(directory.serverOf("search"), "");
  assert.equal(directory.serverOf("evil_search"), "");
  const d2 = new McpToolDirectory();
  const gone = { deref: () => undefined } as any;
  d2.record("github", ["evil_search"], {});
  (d2 as any).owners.set("github", gone);
  d2.record("github", ["search"], real);
  assert.equal(d2.serverOf("evil_search"), "");
  assert.equal(d2.serverOf("search"), "github");
});
