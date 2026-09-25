import { patchAsyncMethod, llmInput } from "./common.js";
export function instrumentOllama(ollama: any): boolean {
  return patchAsyncMethod(ollama, "chat", { sdk: "ollama", provider: "ollama", operation: "chat", input: llmInput });
}
