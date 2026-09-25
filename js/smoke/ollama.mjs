import ollama from "ollama";
import {
  register,
  shutdown,
  instrumentOllama,
  JsonlExporter
} from "../dist/index.js";

register({
  project: "ollama-js-smoke",
  environment: "local",
  exporters: [
    new JsonlExporter("./logs/ollama-js.jsonl")
  ],
  capturePrompt: false,
  captureResponse: false,
  redact: true
});

instrumentOllama(ollama);

try {
  const response = await ollama.chat({
    model: "argus-qwen25-14b-toolplan:latest",
    messages: [
      {
        role: "user",
        content: "Reply only with OK"
      }
    ]
  });

  console.log("MODEL RESPONSE:");
  console.log(response.message?.content);
} finally {
  await shutdown();
}
