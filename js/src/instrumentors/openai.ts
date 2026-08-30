import { patchAsyncMethod, llmInput } from "./common.js";

export function instrumentOpenAI(client: any): boolean {
  let ok = false;
  ok = patchAsyncMethod(client?.responses, "create", { sdk: "openai", provider: "openai", operation: "responses.create", input: llmInput }) || ok;
  ok = patchAsyncMethod(client?.chat?.completions, "create", { sdk: "openai", provider: "openai", operation: "chat.completions.create", input: llmInput }) || ok;
  ok = patchAsyncMethod(client?.embeddings, "create", { sdk: "openai", provider: "openai", operation: "embeddings.create", input: llmInput }) || ok;
  return ok;
}
