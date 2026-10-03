import { randomUUID } from "node:crypto";
import type { EventRecord, RuntimeConfig } from "./types.js";
import type { TraceContext } from "./types.js";
import { deriveAgentId } from "./identity.js";

const id = (prefix: string) => `${prefix}_${randomUUID().replaceAll("-", "")}`;

// 受動計装は run の範囲を張らないため、文脈にも設定にも run_id が無いことがある。Argus の agent-run の
// 検知は run_id の無い記録を評価せずに捨てるため、プロセスの寿命で変わらない値を最後の既定にする。
// Python の計装 (core/runtime.py の _process_run_id) と同じ扱い。
let processRunId: string | undefined;
export function fallbackRunId(): string {
  processRunId ??= id("run");
  return processRunId;
}

export function runtimeMetadata(): Record<string, unknown> {
  return {
    language: "javascript",
    runtime: "node",
    node_version: process.version,
    platform: process.platform,
    arch: process.arch,
    pid: process.pid
  };
}

export function newEvent(args: {
  config: RuntimeConfig;
  context: TraceContext;
  eventType: string;
  source?: Record<string, unknown>;
  actor?: Record<string, unknown>;
  data?: Record<string, unknown>;
  status?: string;
  latencyMs?: number;
  error?: Record<string, unknown>;
  purposeId?: string;
  agentId?: string;
}): EventRecord {
  const { config, context, source = {}, actor, data = {}, status, latencyMs, error, purposeId, agentId } = args;
  return {
    schema_version: "0.2",
    event_id: id("evt"),
    trace_id: context.traceId ?? id("trace"),
    span_id: id("span"),
    parent_span_id: context.spanId ?? null,
    timestamp: new Date().toISOString(),
    project: config.project,
    environment: config.environment,
    event_type: args.eventType,
    tenant_id: config.tenantId ?? null,
    session_id: config.sessionId ?? null,
    conversation_id: config.conversationId ?? null,
    run_id: context.runId ?? config.runId ?? fallbackRunId(),
    turn_id: context.turnId ?? config.turnId ?? null,
    agent_id: agentId ?? context.agentId ?? config.agentId ?? deriveAgentId(config.project, config.environment, String(source.sdk ?? "unknown"), config.agentHint),
    purpose_id: purposeId ?? context.purposeId ?? config.purposeId ?? null,
    source,
    actor: actor ?? config.actor,
    data,
    security: { redacted: false },
    status: status ?? null,
    latency_ms: latencyMs ?? null,
    error: error ?? null,
    runtime: runtimeMetadata(),
    run_environment: validRunEnvironment(config.runEnvironment)
  };
}

// 実行環境の札として許す値。受け取り側の値と同じ並びにする。
const RUN_ENVIRONMENTS = new Set(["production", "staging", "test", "evaluation"]);

export function validRunEnvironment(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const tag = value.trim().toLowerCase();
  return RUN_ENVIRONMENTS.has(tag) ? tag : null;
}
