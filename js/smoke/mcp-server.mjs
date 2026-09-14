import {
  Server
} from "@modelcontextprotocol/sdk/server/index.js";

import {
  StdioServerTransport
} from "@modelcontextprotocol/sdk/server/stdio.js";

import {
  CallToolRequestSchema,
  ListToolsRequestSchema
} from "@modelcontextprotocol/sdk/types.js";

const server = new Server(
  {
    name: "senda-mcp-smoke-server",
    version: "1.0.0"
  },
  {
    capabilities: {
      tools: {}
    }
  }
);

server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: "lookup",
        description: "Simple MCP smoke test tool",
        inputSchema: {
          type: "object",
          properties: {
            query: {
              type: "string"
            }
          },
          required: ["query"]
        }
      }
    ]
  };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name !== "lookup") {
    throw new Error("Unknown tool");
  }

  const query = request.params.arguments?.query ?? "";

  return {
    content: [
      {
        type: "text",
        text: `lookup result: ${query}`
      }
    ]
  };
});

const transport = new StdioServerTransport();

await server.connect(transport);
