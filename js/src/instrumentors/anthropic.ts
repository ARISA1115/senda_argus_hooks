import { patchAsyncMethod, llmInput } from "./common.js";
export function instrumentAnthropic(client: any): boolean {
  return patchAsyncMethod(client?.messages, "create", { sdk: "anthropic", provider: "anthropic", operation: "messages.create", input: llmInput });
}
