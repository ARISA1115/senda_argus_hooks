import {
  Agent,
  run,
  addTraceProcessor
} from "@openai/agents";

import {
  register,
  shutdown,
  JsonlExporter,
  SendaArgusOpenAIAgentsProcessor
} from "../dist/index.js";

register({
  project: "openai-agents-js-smoke",
  environment: "local",
  exporters: [
    new JsonlExporter("./logs/openai-agents-js.jsonl")
  ],
  capturePrompt: false,
  captureResponse: false,
  captureArguments: false,
  captureResult: false,
  redact: true
});

const processor = new SendaArgusOpenAIAgentsProcessor();

addTraceProcessor(processor);

const agent = new Agent({
  name: "Senda smoke agent",
  instructions: "Reply only with OK."
});

try {
  await run(
    agent,
    "Reply only with OK",
    {
      maxTurns: 1
    }
  );
} catch (error) {
  console.log("EXPECTED ERROR PATH:");
  console.log(
    error?.constructor?.name,
    error?.message
  );
} finally {
  await processor.forceFlush?.();
  await shutdown();
}
