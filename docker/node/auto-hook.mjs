import { pathToFileURL } from "node:url";

const enabled = !["0", "false", "no", "off"].includes(String(process.env.SENDA_ARGUS_ENABLED ?? "true").toLowerCase());
if (enabled) {
  try {
    const sdk = await import(pathToFileURL("/opt/senda/argus-hooks/dist/index.js").href);
    const exporter = process.env.SENDA_ARGUS_EXPORTER ?? "jsonl";
    const exporters = exporter.split(",").map((name) => name.trim()).filter(Boolean).map((name) => {
      if (name === "stdout") return { type: "stdout" };
      if (name === "null") return { type: "null" };
      return { type: "jsonl", path: process.env.SENDA_ARGUS_JSONL_PATH ?? "/var/log/senda-argus/events.jsonl" };
    });

    // Configure the runtime automatically. The current JS SDK still instruments
    // OpenAI/Anthropic/MCP client *instances*. Automatic client discovery will be
    // added in the next Zero-code Auto Hook phase; this preload deliberately
    // does not claim coverage it cannot guarantee.
    sdk.register({
      project: process.env.SENDA_ARGUS_PROJECT ?? "default",
      environment: process.env.SENDA_ARGUS_ENVIRONMENT ?? "prod",
      exporters,
      capturePrompt: /^(1|true|yes|on)$/i.test(process.env.SENDA_ARGUS_CAPTURE_PROMPT ?? "false"),
      captureResponse: /^(1|true|yes|on)$/i.test(process.env.SENDA_ARGUS_CAPTURE_RESPONSE ?? "false"),
      captureArguments: /^(1|true|yes|on)$/i.test(process.env.SENDA_ARGUS_CAPTURE_ARGUMENTS ?? "false"),
      captureResult: /^(1|true|yes|on)$/i.test(process.env.SENDA_ARGUS_CAPTURE_RESULT ?? "false"),
      captureHash: !/^(0|false|no|off)$/i.test(process.env.SENDA_ARGUS_CAPTURE_HASH ?? "true"),
      redact: !/^(0|false|no|off)$/i.test(process.env.SENDA_ARGUS_REDACT ?? "true"),
      tenantId: process.env.SENDA_ARGUS_TENANT_ID,
      sessionId: process.env.SENDA_ARGUS_SESSION_ID,
      conversationId: process.env.SENDA_ARGUS_CONVERSATION_ID,
      runId: process.env.SENDA_ARGUS_RUN_ID,
      turnId: process.env.SENDA_ARGUS_TURN_ID,
      agentId: process.env.SENDA_ARGUS_AGENT_ID,
      purposeId: process.env.SENDA_ARGUS_PURPOSE_ID,
      agentHint: process.env.SENDA_ARGUS_AGENT_HINT,
    });

    if (/^(1|true|yes|on)$/i.test(process.env.SENDA_ARGUS_BOOTSTRAP_DEBUG ?? "false")) {
      console.error("[senda-argus] Node runtime configured by preload");
    }
  } catch (error) {
    if (/^(1|true|yes|on)$/i.test(process.env.SENDA_ARGUS_BOOTSTRAP_DEBUG ?? "false")) {
      console.error("[senda-argus] Node preload failed open:", error);
    }
  }
}
