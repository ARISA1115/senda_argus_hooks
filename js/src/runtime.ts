import { getContext, runWithContext, newRunId } from "./core/context.js";
import { newEvent } from "./core/event.js";
import { redactEvent } from "./core/redaction.js";
import type { Exporter, RegisterOptions, RuntimeConfig } from "./core/types.js";
import { JsonlExporter } from "./exporters/jsonl.js";

let config: RuntimeConfig = {
  project: "default", environment: "dev", capturePrompt: false, captureResponse: false,
  captureArguments: false, captureResult: false, captureHash: true, redact: true, actor: {}
};
let exporters: Exporter[] = [];

export function configure(options: RegisterOptions = {}): void {
  config = {
    project: options.project ?? "default",
    environment: options.environment ?? "dev",
    capturePrompt: options.capturePrompt ?? false,
    captureResponse: options.captureResponse ?? false,
    captureArguments: options.captureArguments ?? false,
    captureResult: options.captureResult ?? false,
    captureHash: options.captureHash ?? true,
    redact: options.redact ?? true,
    actor: options.actor ?? {},
    tenantId: options.tenantId, sessionId: options.sessionId,
    conversationId: options.conversationId, runId: options.runId,
    turnId: options.turnId, agentId: options.agentId,
    purposeId: options.purposeId, agentHint: options.agentHint
  };
  exporters = options.exporters?.length ? options.exporters : [new JsonlExporter()];
}

export function getConfig(): RuntimeConfig { return config; }

export function emitEvent(eventType: string, args: {
  data?: Record<string, unknown>; source?: Record<string, unknown>; actor?: Record<string, unknown>;
  status?: string; latencyMs?: number; error?: Record<string, unknown>; purposeId?: string; agentId?: string;
} = {}) {
  let event = newEvent({
    config, context: getContext(), eventType, source: args.source, actor: args.actor,
    data: args.data, status: args.status, latencyMs: args.latencyMs, error: args.error,
    purposeId: args.purposeId, agentId: args.agentId
  });
  if (config.redact) event = redactEvent(event);
  for (const exporter of exporters) void exporter.emit(event);
  return event;
}

export async function flush(): Promise<void> {
  for (const exporter of exporters) await exporter.flush?.();
}
export async function shutdown(): Promise<void> {
  await flush();
  for (const exporter of exporters) await exporter.shutdown?.();
}

export function withTrace<T>(fn: () => T, context: Record<string, string | undefined> = {}): T {
  return runWithContext({ runId: newRunId(), ...context }, fn);
}
