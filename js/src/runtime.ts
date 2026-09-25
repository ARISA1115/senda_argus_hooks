import { randomUUID } from "node:crypto";
import { getContext, runWithContext, newRunId } from "./core/context.js";
import { newEvent, runtimeMetadata } from "./core/event.js";
import { sanitizeEvent } from "./core/redaction.js";
import type { EventRecord, Exporter, ExporterConfig, RegisterOptions, RuntimeConfig } from "./core/types.js";
import { JsonlExporter } from "./exporters/jsonl.js";
import { StdoutExporter } from "./exporters/stdout.js";
import { NullExporter } from "./exporters/null.js";

let config: RuntimeConfig = {
  project: "default", environment: "dev", capturePrompt: false, captureResponse: false,
  captureArguments: false, captureResult: false, captureHash: true, redact: true, actor: {}
};
let exporters: Exporter[] = [];

function isExporter(value: Exporter | ExporterConfig): value is Exporter {
  return typeof (value as Exporter)?.emit === "function";
}

function exporterFromConfig(value: Exporter | ExporterConfig): Exporter {
  if (isExporter(value)) return value;
  switch (value.type) {
    case "jsonl": return new JsonlExporter(value.path);
    case "stdout": return new StdoutExporter();
    case "null": return new NullExporter();
    default: throw new Error(`Unsupported exporter type: ${String((value as any)?.type)}`);
  }
}

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
  exporters = options.exporters?.length
    ? options.exporters.map(exporterFromConfig)
    : [new JsonlExporter()];
}

export function getConfig(): RuntimeConfig { return config; }

// 観測の失敗でホストアプリの呼び出しを変えない。同期の例外も、失敗した Promise も受け止める。
function contain(run: () => void | Promise<void> | undefined): Promise<void> | undefined {
  let pending: void | Promise<void> | undefined;
  try {
    pending = run();
  } catch {
    return undefined;
  }
  if (pending && typeof (pending as PromiseLike<void>).then === "function") {
    return Promise.resolve(pending).catch(() => undefined);
  }
  return undefined;
}

type EmitArgs = {
  data?: Record<string, unknown>; source?: Record<string, unknown>; actor?: Record<string, unknown>;
  status?: string; latencyMs?: number; error?: Record<string, unknown>; purposeId?: string; agentId?: string;
};

function buildEvent(eventType: string, args: EmitArgs): EventRecord {
  const event = newEvent({
    config, context: getContext(), eventType, source: args.source, actor: args.actor,
    data: args.data, status: args.status, latencyMs: args.latencyMs, error: args.error,
    purposeId: args.purposeId, agentId: args.agentId
  });
  return sanitizeEvent(event, config.redact);
}

function markFailed(event: EventRecord): EventRecord {
  event.security = { ...event.security, observation_failed: true };
  return event;
}

function lastResortEvent(eventType: string): EventRecord {
  const id = (prefix: string) => `${prefix}_${randomUUID().replaceAll("-", "")}`;
  return {
    schema_version: "0.2", event_id: id("evt"), trace_id: id("trace"), span_id: id("span"),
    parent_span_id: null, timestamp: new Date().toISOString(),
    project: String(config.project), environment: String(config.environment), event_type: String(eventType),
    tenant_id: null, session_id: null, conversation_id: null, run_id: null, turn_id: null,
    agent_id: null, purpose_id: null, source: {}, actor: {}, data: {},
    security: { redacted: config.redact, observation_failed: true },
    status: null, latency_ms: null, error: null, runtime: runtimeMetadata()
  };
}

// 事象の中身を組み立てられなくても、起きたこと自体は残す。落とすものを段階的に増やす。
function buildEventContained(eventType: string, args: EmitArgs): EventRecord {
  try {
    return buildEvent(eventType, args);
  } catch {
    // 本文が原因なら本文だけを空にする
  }
  try {
    return markFailed(buildEvent(eventType, { ...args, data: {} }));
  } catch {
    // 本文以外の項目が原因なら、文字列の項目だけを残す
  }
  try {
    return markFailed(buildEvent(eventType, {
      data: {},
      source: { sdk: String(args.source?.sdk ?? "unknown") },
      status: typeof args.status === "string" ? args.status : undefined,
      latencyMs: typeof args.latencyMs === "number" ? args.latencyMs : undefined,
      purposeId: typeof args.purposeId === "string" ? args.purposeId : undefined,
      agentId: typeof args.agentId === "string" ? args.agentId : undefined
    }));
  } catch {
    return lastResortEvent(eventType);
  }
}

export function emitEvent(eventType: string, args: EmitArgs = {}): EventRecord {
  const event = buildEventContained(eventType, args);
  for (const exporter of exporters) void contain(() => exporter.emit(event));
  return event;
}

// 計装した呼び出しの結果を観測する処理を包む。観測の失敗は呼び出しへ返さない。
export function observe(run: () => void): void {
  try {
    run();
  } catch {
    // 観測の失敗でホストアプリの振る舞いを変えない
  }
}

export async function flush(): Promise<void> {
  for (const exporter of exporters) await contain(() => exporter.flush?.());
}
export async function shutdown(): Promise<void> {
  await flush();
  for (const exporter of exporters) await contain(() => exporter.shutdown?.());
}

export function withTrace<T>(fn: () => T, context: Record<string, string | undefined> = {}): T {
  return runWithContext({ runId: newRunId(), ...context }, fn);
}
