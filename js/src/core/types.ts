export type JsonLike = unknown;

export interface TraceContext {
  traceId?: string;
  spanId?: string;
  runId?: string;
  turnId?: string;
  agentId?: string;
  purposeId?: string;
}

export interface EventRecord {
  schema_version: "0.2";
  event_id: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  timestamp: string;
  project: string;
  environment: string;
  event_type: string;
  tenant_id: string | null;
  session_id: string | null;
  conversation_id: string | null;
  run_id: string | null;
  turn_id: string | null;
  agent_id: string | null;
  purpose_id: string | null;
  source: Record<string, unknown>;
  actor: Record<string, unknown>;
  data: Record<string, unknown>;
  security: Record<string, unknown>;
  status: string | null;
  latency_ms: number | null;
  error: Record<string, unknown> | null;
  runtime: Record<string, unknown>;
  // 実行環境の札。production と staging と test と evaluation に限る。設定に無い値は載せない。
  run_environment?: string | null;
}

export interface Exporter {
  emit(event: EventRecord): void | Promise<void>;
  flush?(): void | Promise<void>;
  shutdown?(): void | Promise<void>;
}

export type ExporterConfig =
  | { type: "jsonl"; path?: string }
  | { type: "stdout" }
  | { type: "null" }
  | { type: "argus"; endpoint?: string; apiKey?: string; runId?: string; timeoutMs?: number };

export interface RegisterOptions {
  project?: string;
  environment?: string;
  runEnvironment?: string;
  exporters?: Array<Exporter | ExporterConfig>;
  capturePrompt?: boolean;
  captureResponse?: boolean;
  captureArguments?: boolean;
  captureResult?: boolean;
  // 戻り値の本文とは別に、検知の走査だけに使う文を送る。Argus は保存せず判定の後に捨てる。
  scanResult?: boolean;
  captureHash?: boolean;
  redact?: boolean;
  actor?: Record<string, unknown>;
  tenantId?: string;
  sessionId?: string;
  conversationId?: string;
  runId?: string;
  turnId?: string;
  agentId?: string;
  purposeId?: string;
  agentHint?: string;
}

export interface RuntimeConfig extends Required<Omit<RegisterOptions,
  "exporters" | "tenantId" | "sessionId" | "conversationId" | "runId" | "turnId" | "agentId" | "purposeId" | "agentHint" | "runEnvironment">> {
  runEnvironment?: string;
  tenantId?: string;
  sessionId?: string;
  conversationId?: string;
  runId?: string;
  turnId?: string;
  agentId?: string;
  purposeId?: string;
  agentHint?: string;
}
