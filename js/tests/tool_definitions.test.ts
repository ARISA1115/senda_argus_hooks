import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { register } from "../src/register.js";
import { shutdown } from "../src/runtime.js";
import { foldName, MAX_NAME_LEN, MAX_TOOLS_PER_SERVER } from "../src/core/mcp_tools.js";
import { normalizeProviderUrl, toolDefinitionHash, toolDefinitionHashes } from "../src/core/tool_definitions.js";
import type { EventRecord, Exporter } from "../src/core/types.js";

// ツールの定義のダイジェストの規則を固定する。規則は Python の実装と同じで、両方のテストが同じ例
// (tests/fixtures/mcp_tool_definition_hashes.json) を読む。

class MemoryExporter implements Exporter {
  events: EventRecord[] = [];
  emit(event: EventRecord) { this.events.push(event); }
}

type Fixture = { cases: Array<{ name: string; tool: any; hash: string }> };
const fixture: Fixture = JSON.parse(readFileSync(join(process.cwd(), "tests", "fixtures", "mcp_tool_definition_hashes.json"), "utf8"));

for (const c of fixture.cases) {
  test(`definition hash matches the shared fixture: ${c.name}`, () => {
    assert.equal(toolDefinitionHash(c.tool), c.hash);
  });
}

test("hashes are capped and long names are folded", () => {
  const longName = "x".repeat(MAX_NAME_LEN + 1);
  const tools = [{ name: longName }, ...Array.from({ length: MAX_TOOLS_PER_SERVER + 10 }, (_, i) => ({ name: `t${i}` }))];
  const hashes = toolDefinitionHashes({ tools });
  assert.equal(Object.keys(hashes).length, MAX_TOOLS_PER_SERVER);
  assert.ok(foldName(longName) in hashes);
  assert.ok(!(longName in hashes));
});

test("a tool named __proto__ is kept as a key", () => {
  const hashes = toolDefinitionHashes({ tools: [{ name: "__proto__" }] });
  assert.deepEqual(Object.keys(hashes), ["__proto__"]);
});

test("listTools emits the definition hashes with the server url", async () => {
  const sink = new MemoryExporter();
  const client = {
    callTool: async () => ({}),
    listTools: async () => ({ tools: [fixture.cases[0].tool] })
  };
  register({ project: "definitions", exporters: [sink] }, { mcp: client, mcpMetadata: { serverName: "demo", serverUrl: "https://MCP.EXAMPLE.com/mcp/" } });
  try {
    await client.listTools();
  } finally {
    await shutdown();
  }
  const listed = sink.events.filter((e) => e.event_type === "mcp.list_tools.completed");
  assert.equal(listed.length, 1);
  const mcp = (listed[0].data as any).mcp;
  assert.deepEqual({ ...mcp.tool_definition_hashes }, { lookup_order: fixture.cases[0].hash });
  assert.equal(mcp.server_url, "https://mcp.example.com/mcp");
});

test("an empty listing emits nothing", async () => {
  const sink = new MemoryExporter();
  const client = { callTool: async () => ({}), listTools: async () => ({ tools: [] }) };
  register({ project: "definitions-empty", exporters: [sink] }, { mcp: client, mcpMetadata: { serverName: "demo" } });
  try {
    await client.listTools();
  } finally {
    await shutdown();
  }
  assert.equal(sink.events.filter((e) => e.event_type === "mcp.list_tools.completed").length, 0);
});

type UrlCase = { input: string; expected: string | null };
const urlCases: UrlCase[] = JSON.parse(readFileSync(join(process.cwd(), "tests", "fixtures", "mcp_tool_definition_hashes.json"), "utf8")).urls;

for (const c of urlCases) {
  test(`provider url matches the shared fixture: ${c.input || "empty"}`, () => {
    assert.equal(normalizeProviderUrl(c.input), c.expected);
  });
}
