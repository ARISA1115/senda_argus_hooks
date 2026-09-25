import {
  Document,
  VectorStoreIndex,
  Settings
} from "llamaindex";

import {
  OllamaEmbedding
} from "@llamaindex/ollama";

import {
  register,
  shutdown,
  JsonlExporter,
  instrumentLlamaIndex
} from "../dist/index.js";

register({
  project: "llamaindex-js-smoke",
  environment: "local",
  exporters: [
    new JsonlExporter("./logs/llamaindex-js.jsonl")
  ],
  captureArguments: false,
  captureResult: false,
  redact: true
});

Settings.embedModel = new OllamaEmbedding({
  model: "nomic-embed-text",
  config: {
    host: "http://127.0.0.1:11434"
  }
});

const documents = [
  new Document({
    text: "Senda-Argus Hooks collects normalized observability events from AI SDKs."
  }),
  new Document({
    text: "CVE-2024-3094 is associated with the XZ Utils supply-chain incident."
  })
];

try {
  const index = await VectorStoreIndex.fromDocuments(documents);

  const retriever = index.asRetriever({
    similarityTopK: 2
  });

  const changed = instrumentLlamaIndex({
    retriever
  });

  console.log("INSTRUMENTED:");
  console.dir(changed, { depth: null });

  const nodes = await retriever.retrieve({
    query: "What does Senda-Argus Hooks collect?"
  });

  console.log("RETRIEVED:");
  console.dir(
    nodes.map((node) => ({
      score: node.score,
      text: node.node?.getText?.()
    })),
    { depth: null }
  );

} finally {
  await shutdown();
}
