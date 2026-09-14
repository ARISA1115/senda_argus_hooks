import {
  Client
} from "@modelcontextprotocol/sdk/client/index.js";

import {
  StdioClientTransport
} from "@modelcontextprotocol/sdk/client/stdio.js";

import {
  register,
  shutdown,
  instrumentMCP,
  JsonlExporter
} from "../dist/index.js";

register({
  project: "mcp-js-smoke",
  environment: "local",

  exporters: [
    new JsonlExporter("./logs/mcp-js.jsonl")
  ],

  captureArguments: true,
  captureResult: true,
  redact: true
});

const client = new Client(
  {
    name: "senda-mcp-smoke-client",
    version: "1.0.0"
  },
  {
    capabilities: {}
  }
);

instrumentMCP(client, {
  serverName: "senda-mcp-smoke-server",
  serverUrl: "stdio://local",
  capability: "vulnerability_intelligence"
});

const transport = new StdioClientTransport({
  command: process.execPath,
  args: ["./smoke/mcp-server.mjs"]
});

try {
  await client.connect(transport);

  const result = await client.callTool({
    name: "lookup",
    arguments: {
      query: "CVE-2024-3094"
    }
  });

  console.log("MCP RESULT:");
  console.dir(result, {
    depth: null
  });

} finally {
  await client.close();
  await shutdown();
}
