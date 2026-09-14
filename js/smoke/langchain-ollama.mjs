import { ChatOllama } from "@langchain/ollama";

import {
  register,
  shutdown,
  JsonlExporter,
  langChainCallbackHandler
} from "../dist/index.js";

register({
  project: "langchain-ollama-js-smoke",
  environment: "local",
  exporters: [
    new JsonlExporter("./logs/langchain-ollama-js.jsonl")
  ],
  capturePrompt: false,
  captureResponse: false,
  captureArguments: false,
  captureResult: false,
  redact: true
});

const handler = langChainCallbackHandler();

const model = new ChatOllama({
  model: "argus-qwen25-14b-toolplan:latest",
  baseUrl: "http://127.0.0.1:11434",
  temperature: 0
});

try {
  const result = await model.invoke(
    "Reply only with OK",
    {
      callbacks: [handler]
    }
  );

  console.log("LANGCHAIN RESPONSE:");
  console.log(result.content);
} finally {
  await shutdown();
}
