import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";
import type { TraceContext } from "./types.js";

const storage = new AsyncLocalStorage<TraceContext>();

export function getContext(): TraceContext {
  return storage.getStore() ?? {};
}

export function runWithContext<T>(context: TraceContext, fn: () => T): T {
  return storage.run({ ...getContext(), ...context }, fn);
}

export function newRunId(): string {
  return `run_${randomUUID().replaceAll("-", "")}`;
}

export function newTraceId(): string {
  return `trace_${randomUUID().replaceAll("-", "")}`;
}
