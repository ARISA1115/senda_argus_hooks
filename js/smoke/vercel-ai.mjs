import {
  generateText,
  wrapLanguageModel
} from "ai";

import {
  register,
  shutdown,
  JsonlExporter,
  sendaArgusLanguageModelMiddleware
} from "../dist/index.js";

register({
  project: "vercel-ai-js-smoke",
  environment: "local",
  exporters: [
    new JsonlExporter("./logs/vercel-ai-js.jsonl")
  ],
  capturePrompt: false,
  captureResponse: false,
  redact: true
});

const fakeModel = {
  specificationVersion: "v4",
  provider: "senda-smoke",
  modelId: "fake-model",

  supportedUrls: {},

  async doGenerate(options) {
    return {
      content: [
        {
          type: "text",
          text: "OK"
        }
      ],

      finishReason: "stop",

      usage: {
        inputTokens: {
          total: 1,
          noCache: 1,
          cacheRead: 0,
          cacheWrite: 0
        },
        outputTokens: {
          total: 1,
          text: 1,
          reasoning: 0
        }
      },

      warnings: [],

      response: {
        id: "response-smoke",
        timestamp: new Date(),
        modelId: "fake-model"
      }
    };
  },

  async doStream() {
    throw new Error("stream not used");
  }
};

const model = wrapLanguageModel({
  model: fakeModel,
  middleware: sendaArgusLanguageModelMiddleware()
});

try {
  const result = await generateText({
    model,
    prompt: "Reply only with OK"
  });

  console.log("VERCEL AI RESULT:");
  console.log(result.text);

} finally {
  await shutdown();
}
