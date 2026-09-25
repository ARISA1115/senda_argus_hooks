import { StateGraph, Annotation } from "@langchain/langgraph";
import { ChatOllama } from "@langchain/ollama";

import {
  register,
  shutdown,
  JsonlExporter,
  invokeWithArgus
} from "../dist/index.js";

register({
  project: "langgraph-ollama-js-smoke",
  environment: "local",
  exporters: [
    new JsonlExporter("./logs/langgraph-ollama-js.jsonl")
  ],
  capturePrompt: false,
  captureResponse: false,
  captureArguments: false,
  captureResult: false,
  redact: true
});

const State = Annotation.Root({
  input: Annotation(),
  output: Annotation()
});

const model = new ChatOllama({
  model: "argus-qwen25-14b-toolplan:latest",
  baseUrl: "http://127.0.0.1:11434",
  temperature: 0
});

async function callModel(state) {
  const result = await model.invoke(
    `Reply only with OK. Input: ${state.input}`
  );

  return {
    output: result.content
  };
}

const graph = new StateGraph(State)
  .addNode("call_model", callModel)
  .addEdge("__start__", "call_model")
  .addEdge("call_model", "__end__")
  .compile();

try {
  const result = await invokeWithArgus(
    graph,
    {
      input: "LangGraph smoke test"
    }
  );

  console.log("LANGGRAPH RESULT:");
  console.dir(result, { depth: null });

} finally {
  await shutdown();
}
