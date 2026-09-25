const VERSION = "0.2.0";
const SCHEMA_VERSION = "0.2";

const REDACT_FIELDS = new Set([
  "authorization", "api_key", "apikey", "password", "secret", "token",
  "access_token", "refresh_token", "cookie", "set-cookie", "x-api-key"
]);

const MCP_METHODS = new Set([
  "initialize",
  "ping",
  "tools/list",
  "tools/call",
  "resources/list",
  "resources/read",
  "resources/subscribe",
  "resources/unsubscribe",
  "prompts/list",
  "prompts/get",
  "logging/setLevel",
  "completion/complete",
  "notifications/initialized",
  "notifications/cancelled",
  "notifications/progress",
  "notifications/resources/list_changed",
  "notifications/resources/updated",
  "notifications/tools/list_changed",
  "notifications/prompts/list_changed"
]);

const state = {
  config: null,
  installed: false,
  originals: {},
  buffer: [],
  flushTimer: null,
  listeners: [],
  traceId: null,
  runId: null,
  events: []
};

function uid(prefix) {
  const c = globalThis.crypto;
  if (c?.randomUUID) return `${prefix}_${c.randomUUID().replaceAll("-", "")}`;
  return `${prefix}_${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`;
}

function stableStringify(value) {
  const seen = new WeakSet();
  const normalize = (v) => {
    if (v === undefined) return null;
    if (v === null || typeof v !== "object") return v;
    if (seen.has(v)) return "[Circular]";
    seen.add(v);
    if (Array.isArray(v)) return v.map(normalize);
    return Object.keys(v).sort().reduce((out, key) => {
      out[key] = normalize(v[key]);
      return out;
    }, {});
  };
  return JSON.stringify(normalize(value));
}

async function sha256(value) {
  const text = typeof value === "string" ? value : stableStringify(value);
  if (globalThis.crypto?.subtle) {
    const data = new TextEncoder().encode(text);
    const digest = await globalThis.crypto.subtle.digest("SHA-256", data);
    return `sha256:${[...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
  }
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
  return `fnv1a:${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

function redactString(value) {
  return String(value)
    .replace(/sk-[A-Za-z0-9_-]{12,}/g, "***REDACTED***")
    .replace(/AKIA[0-9A-Z]{16}/g, "***REDACTED***")
    .replace(/(bearer\s+)[A-Za-z0-9._-]+/gi, "$1***REDACTED***");
}

function redact(value) {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      out[key] = REDACT_FIELDS.has(key.toLowerCase()) ? "***REDACTED***" : redact(item);
    }
    return out;
  }
  return typeof value === "string" ? redactString(value) : value;
}

function normalizeUrl(raw) {
  try {
    const url = new URL(String(raw), globalThis.location?.href);
    return `${url.protocol}//${url.host}${url.pathname.replace(/\/$/, "") || "/"}`;
  } catch {
    return String(raw || "");
  }
}

function defaultConfig(options = {}) {
  return {
    project: "default",
    environment: "dev",
    endpoint: null,
    exporter: "http",
    headers: {},
    batchSize: 10,
    flushIntervalMs: 2000,
    capturePrompt: false,
    captureResponse: false,
    captureArguments: false,
    captureResult: false,
    captureHash: true,
    redact: true,
    maxBodyBytes: 256000,
    actor: {},
    tenantId: null,
    sessionId: null,
    conversationId: null,
    runId: null,
    turnId: null,
    agentId: null,
    purposeId: null,
    agentHint: "browser-agent",
    instrumentFetch: true,
    instrumentXHR: true,
    instrumentWebSocket: true,
    includeUrlPatterns: [],
    excludeUrlPatterns: [],
    useBeacon: true,
    debug: false,
    ...options
  };
}

function runtimeMetadata() {
  return {
    language: "javascript",
    sdk: "senda_argus_browser_hooks",
    sdk_version: VERSION,
    user_agent: globalThis.navigator?.userAgent,
    page_origin: globalThis.location?.origin
  };
}

async function deriveAgentId(source = {}) {
  if (state.config.agentId) return state.config.agentId;
  const hash = await sha256({
    project: state.config.project,
    environment: state.config.environment,
    sdk: source.sdk || "browser",
    agent_hint: state.config.agentHint
  });
  return `agent_${hash.split(":")[1].slice(0, 16)}`;
}

async function emit(eventType, {
  data = {}, source = {}, status = null, latencyMs = null, error = null,
  purposeId = null, parentSpanId = null
} = {}) {
  if (!state.config) return null;
  state.traceId ||= uid("trace");
  state.runId ||= state.config.runId || uid("run");

  let event = {
    schema_version: SCHEMA_VERSION,
    event_id: uid("evt"),
    trace_id: state.traceId,
    span_id: uid("span"),
    parent_span_id: parentSpanId,
    timestamp: new Date().toISOString(),
    project: state.config.project,
    environment: state.config.environment,
    event_type: eventType,
    tenant_id: state.config.tenantId,
    session_id: state.config.sessionId,
    conversation_id: state.config.conversationId,
    run_id: state.runId,
    turn_id: state.config.turnId,
    agent_id: await deriveAgentId(source),
    purpose_id: purposeId || state.config.purposeId,
    source,
    actor: state.config.actor || {},
    data,
    security: {redacted: false},
    status,
    latency_ms: latencyMs,
    error,
    runtime: runtimeMetadata()
  };

  if (state.config.redact) {
    event = redact(event);
    event.security.redacted = true;
  }

  state.events.push(event);
  state.buffer.push(event);
  if (state.config.debug) console.debug("[SendaArgus]", eventType, event);
  if (state.buffer.length >= state.config.batchSize) void flush();
  return event;
}

async function flush() {
  if (!state.config || !state.buffer.length) return;
  const events = state.buffer.splice(0, state.buffer.length);
  try {
    if (state.config.exporter === "console") {
      console.log("[SendaArgus events]", events);
      return;
    }
    if (state.config.exporter === "memory") return;
    if (!state.config.endpoint) return;

    const body = JSON.stringify({events});
    if (globalThis.navigator?.sendBeacon && state.config.useBeacon !== false) {
      const ok = globalThis.navigator.sendBeacon(
        state.config.endpoint,
        new Blob([body], {type: "application/json"})
      );
      if (ok) return;
    }

    const rawFetch = state.originals.fetch || globalThis.fetch;
    await rawFetch(state.config.endpoint, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...state.config.headers,
        "x-senda-argus-internal": "1"
      },
      body,
      keepalive: true
    });
  } catch {
    state.buffer.unshift(...events.slice(-100));
  }
}

function shouldObserveUrl(url) {
  const value = String(url || "");
  if (state.config.excludeUrlPatterns.some((pattern) => new RegExp(pattern).test(value))) return false;
  if (!state.config.includeUrlPatterns.length) return true;
  return state.config.includeUrlPatterns.some((pattern) => new RegExp(pattern).test(value));
}

function isMcpMethod(method) {
  return typeof method === "string" && MCP_METHODS.has(method);
}

function classifyRequest(url, body, headers = {}) {
  const value = String(url || "").toLowerCase();
  const object = body && typeof body === "object" ? body : null;

  if (object?.jsonrpc === "2.0" && isMcpMethod(object.method)) return "mcp";
  if (/\/mcp(?:\/|$)|message\?sessionid/.test(value) && object?.jsonrpc === "2.0") return "mcp";

  if (/\/api\/(chat|generate|embed|embeddings)(?:\?|$)/.test(value) || /:11434\/(?:api|v1)\//.test(value)) {
    return "ollama";
  }
  if (/\/(v1\/)?(chat\/completions|responses|embeddings|completions)(?:\?|$)/.test(value)) {
    return "openai_compatible";
  }
  if (/anthropic|\/v1\/messages(?:\?|$)/.test(value) && (headers["anthropic-version"] || object?.messages)) {
    return "anthropic";
  }
  return null;
}

function parseMaybeJson(body) {
  if (body == null) return null;
  if (typeof body === "object" && !(body instanceof ArrayBuffer) && !(body instanceof Blob) && !(body instanceof FormData)) {
    return body;
  }
  if (typeof body !== "string") return null;
  try {
    return JSON.parse(body);
  } catch {
    return null;
  }
}

function headersObject(headers) {
  const out = {};
  try {
    new Headers(headers || {}).forEach((value, key) => {
      out[key] = value;
    });
  } catch {}
  return out;
}

async function llmRequestData(kind, url, body, headers) {
  const messages = body?.messages || body?.input || body?.prompt;
  const data = {
    llm: {
      provider: kind,
      operation: body?.stream ? "stream" : "request",
      model: body?.model,
      endpoint: normalizeUrl(url),
      stream: Boolean(body?.stream),
      messages_count: Array.isArray(body?.messages) ? body.messages.length : undefined,
      input_hash: state.config.captureHash ? await sha256(body) : undefined
    }
  };
  if (state.config.capturePrompt) data.llm.input = body;
  else if (state.config.captureHash && messages != null) data.llm.messages_hash = await sha256(messages);
  if (state.config.captureArguments) data.llm.headers = headers;
  return data;
}

async function mcpRequestData(url, body) {
  const method = body?.method;
  const params = body?.params || {};
  const tool = method === "tools/call" ? params.name : undefined;
  const args = method === "tools/call" ? params.arguments : params;
  const profile = {
    source_type: "mcp",
    mcp_server_url: normalizeUrl(url),
    tool_name: tool || method || "unknown"
  };
  const purposeHash = await sha256(profile);
  const purposeId = `purpose_${purposeHash.split(":")[1].slice(0, 16)}`;
  const mcp = {
    operation: method,
    server_url: normalizeUrl(url),
    tool,
    request_id: body?.id,
    purpose_id: purposeId,
    purpose_source: "mcp_data_source_hash",
    purpose_profile: profile,
    data_source_hash: await sha256(profile),
    arguments_hash: await sha256(args)
  };
  if (state.config.captureArguments) mcp.arguments = args;
  return {data: {mcp}, purposeId};
}

async function instrumentedFetch(input, init = {}) {
  const rawFetch = state.originals.fetch;
  const request = input instanceof Request ? input : null;
  const url = request?.url || input;
  const headers = headersObject(init.headers || request?.headers);

  if (headers["x-senda-argus-internal"] === "1" || !shouldObserveUrl(url)) {
    return rawFetch(input, init);
  }

  let rawBody = init.body;
  if (rawBody == null && request) {
    try {
      rawBody = await request.clone().text();
    } catch {}
  }

  const body = parseMaybeJson(rawBody);
  const kind = classifyRequest(url, body, headers);
  if (!kind) return rawFetch(input, init);

  const start = performance.now();
  let requested;
  let purposeId = null;

  if (kind === "mcp") {
    const meta = await mcpRequestData(url, body);
    purposeId = meta.purposeId;
    requested = await emit(
      body?.method === "tools/call" ? "mcp.tool_call.requested" : "mcp.requested",
      {
        source: {component: "instrumentor", sdk: "browser_fetch", operation: body?.method},
        data: meta.data,
        status: "start",
        purposeId
      }
    );
  } else {
    requested = await emit("llm.request", {
      source: {component: "instrumentor", sdk: "browser_fetch", provider: kind},
      data: await llmRequestData(kind, url, body, headers),
      status: "start"
    });
  }

  try {
    const response = await rawFetch(input, init);
    const latencyMs = Math.round(performance.now() - start);
    let responsePayload = null;

    try {
      const text = await response.clone().text();
      if (text.length <= state.config.maxBodyBytes) responsePayload = parseMaybeJson(text) ?? text;
    } catch {}

    if (kind === "mcp") {
      const data = (await mcpRequestData(url, body)).data;
      if (state.config.captureHash) data.mcp.result_hash = await sha256(responsePayload);
      if (state.config.captureResult) data.mcp.result = responsePayload;
      await emit(body?.method === "tools/call" ? "mcp.tool_call.completed" : "mcp.completed", {
        source: {component: "instrumentor", sdk: "browser_fetch", operation: body?.method},
        data,
        status: response.ok ? "success" : "error",
        latencyMs,
        purposeId,
        parentSpanId: requested?.span_id,
        error: response.ok ? null : {type: "HTTPError", message: `HTTP ${response.status}`}
      });
    } else {
      const data = {llm: {provider: kind, endpoint: normalizeUrl(url), status_code: response.status}};
      if (state.config.captureResponse) data.llm.output = responsePayload;
      if (state.config.captureHash) data.llm.output_hash = await sha256(responsePayload);
      await emit(response.ok ? "llm.response" : "llm.error", {
        source: {component: "instrumentor", sdk: "browser_fetch", provider: kind},
        data,
        status: response.ok ? "success" : "error",
        latencyMs,
        parentSpanId: requested?.span_id,
        error: response.ok ? null : {type: "HTTPError", message: `HTTP ${response.status}`}
      });
    }
    return response;
  } catch (error) {
    const latencyMs = Math.round(performance.now() - start);
    const eventType = kind === "mcp"
      ? (body?.method === "tools/call" ? "mcp.tool_call.failed" : "mcp.failed")
      : "llm.error";
    await emit(eventType, {
      source: {component: "instrumentor", sdk: "browser_fetch", provider: kind},
      data: kind === "mcp"
        ? (await mcpRequestData(url, body)).data
        : await llmRequestData(kind, url, body, headers),
      status: "error",
      latencyMs,
      purposeId,
      parentSpanId: requested?.span_id,
      error: {type: error?.name || "Error", message: error?.message || String(error)}
    });
    throw error;
  }
}

function patchFetch() {
  if (!globalThis.fetch || state.originals.fetch) return;
  state.originals.fetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = instrumentedFetch;
}

function patchXHR() {
  if (!globalThis.XMLHttpRequest || state.originals.xhrOpen) return;
  const prototype = XMLHttpRequest.prototype;
  state.originals.xhrOpen = prototype.open;
  state.originals.xhrSend = prototype.send;
  state.originals.xhrSetRequestHeader = prototype.setRequestHeader;

  prototype.open = function open(method, url, ...rest) {
    this.__senda = {method, url, headers: {}};
    return state.originals.xhrOpen.call(this, method, url, ...rest);
  };

  prototype.setRequestHeader = function setRequestHeader(key, value) {
    if (this.__senda) this.__senda.headers[String(key).toLowerCase()] = value;
    return state.originals.xhrSetRequestHeader.call(this, key, value);
  };

  prototype.send = function send(body) {
    const meta = this.__senda;
    if (!meta || !shouldObserveUrl(meta.url)) return state.originals.xhrSend.call(this, body);

    const object = parseMaybeJson(body);
    const kind = classifyRequest(meta.url, object, meta.headers || {});
    if (!kind) return state.originals.xhrSend.call(this, body);

    const start = performance.now();
    void (async () => {
      const requestMeta = kind === "mcp"
        ? await mcpRequestData(meta.url, object)
        : {data: await llmRequestData(kind, meta.url, object, meta.headers), purposeId: null};
      const startEvent = await emit(
        kind === "mcp"
          ? (object?.method === "tools/call" ? "mcp.tool_call.requested" : "mcp.requested")
          : "llm.request",
        {
          source: {component: "instrumentor", sdk: "browser_xhr", provider: kind},
          data: requestMeta.data,
          status: "start",
          purposeId: requestMeta.purposeId
        }
      );

      this.addEventListener("loadend", async () => {
        const responsePayload = parseMaybeJson(this.responseText) ?? this.responseText;
        const succeeded = this.status >= 200 && this.status < 400;

        if (kind === "mcp") {
          const data = (await mcpRequestData(meta.url, object)).data;
          if (state.config.captureHash) data.mcp.result_hash = await sha256(responsePayload);
          if (state.config.captureResult) data.mcp.result = responsePayload;
          await emit(
            object?.method === "tools/call"
              ? (succeeded ? "mcp.tool_call.completed" : "mcp.tool_call.failed")
              : (succeeded ? "mcp.completed" : "mcp.failed"),
            {
              source: {component: "instrumentor", sdk: "browser_xhr", provider: kind},
              data,
              status: succeeded ? "success" : "error",
              latencyMs: Math.round(performance.now() - start),
              parentSpanId: startEvent?.span_id,
              purposeId: requestMeta.purposeId
            }
          );
        } else {
          const data = {llm: {provider: kind, endpoint: normalizeUrl(meta.url)}};
          if (state.config.captureHash) data.llm.output_hash = await sha256(responsePayload);
          if (state.config.captureResponse) data.llm.output = responsePayload;
          await emit(succeeded ? "llm.response" : "llm.error", {
            source: {component: "instrumentor", sdk: "browser_xhr", provider: kind},
            data,
            status: succeeded ? "success" : "error",
            latencyMs: Math.round(performance.now() - start),
            parentSpanId: startEvent?.span_id
          });
        }
      }, {once: true});
    })();

    return state.originals.xhrSend.call(this, body);
  };
}

function patchWebSocket() {
  if (!globalThis.WebSocket || state.originals.WebSocket) return;
  const NativeWebSocket = globalThis.WebSocket;
  state.originals.WebSocket = NativeWebSocket;

  globalThis.WebSocket = class SendaWebSocket extends NativeWebSocket {
    constructor(url, protocols) {
      super(url, protocols);
      this.__sendaUrl = String(url);
      this.addEventListener("message", (event) => {
        void observeSocketMessage("received", this.__sendaUrl, event.data);
      });
    }

    send(data) {
      void observeSocketMessage("sent", this.__sendaUrl, data);
      return super.send(data);
    }
  };
}

async function observeSocketMessage(direction, url, raw) {
  if (!shouldObserveUrl(url)) return;
  const body = parseMaybeJson(raw);
  const isKnownRequest = body?.jsonrpc === "2.0" && isMcpMethod(body.method);
  const isMcpEndpointResponse = body?.jsonrpc === "2.0" && /\/mcp(?:\/|$)|message\?sessionid/i.test(url);
  if (!isKnownRequest && !isMcpEndpointResponse) return;

  const meta = await mcpRequestData(url, body);
  await emit(`mcp.websocket.${direction}`, {
    source: {component: "instrumentor", sdk: "browser_websocket"},
    data: meta.data,
    status: "success",
    purposeId: meta.purposeId
  });
}

function addListener(target, type, handler, options) {
  if (!target || typeof target.addEventListener !== "function") return;
  target.addEventListener(type, handler, options);
  state.listeners.push([target, type, handler, options]);
}

function register(options = {}) {
  if (state.installed) return api;
  state.config = defaultConfig(options);
  state.installed = true;

  if (state.config.instrumentFetch) patchFetch();
  if (state.config.instrumentXHR) patchXHR();
  if (state.config.instrumentWebSocket) patchWebSocket();

  state.flushTimer = setInterval(() => void flush(), state.config.flushIntervalMs);
  state.flushTimer?.unref?.();
  addListener(globalThis, "pagehide", () => void flush());
  addListener(globalThis, "beforeunload", () => void flush());

  void emit("browser.ai.instrumented", {
    source: {component: "runtime", sdk: "senda_argus_browser_hooks"},
    data: {
      scope: ["llm", "mcp"],
      instrumentors: {
        fetch: Boolean(state.originals.fetch),
        xhr: Boolean(state.originals.xhrOpen),
        websocket: Boolean(state.originals.WebSocket)
      }
    },
    status: "success"
  });
  return api;
}

function unregister() {
  if (!state.installed) return;
  if (state.originals.fetch) globalThis.fetch = state.originals.fetch;
  if (state.originals.xhrOpen) {
    XMLHttpRequest.prototype.open = state.originals.xhrOpen;
    XMLHttpRequest.prototype.send = state.originals.xhrSend;
    XMLHttpRequest.prototype.setRequestHeader = state.originals.xhrSetRequestHeader;
  }
  if (state.originals.WebSocket) globalThis.WebSocket = state.originals.WebSocket;

  for (const [target, type, handler, options] of state.listeners) {
    target.removeEventListener(type, handler, options);
  }
  state.listeners = [];
  clearInterval(state.flushTimer);
  state.installed = false;
  state.originals = {};
  state.flushTimer = null;
  state.traceId = null;
  state.runId = null;
}

function getEvents() {
  return [...state.events];
}

function clearEvents() {
  state.events.length = 0;
  state.buffer.length = 0;
}

function setContext(patch) {
  if (!state.config) return;
  Object.assign(state.config, patch || {});
}

function autoRegisterFromScript() {
  if (!globalThis.document) return;
  const script = document.currentScript
    || [...document.scripts].find((item) => /senda-argus-browser-hooks/.test(item.src));
  if (!script || script.dataset.auto === "false") return;

  const bool = (name, fallback) => script.dataset[name] == null
    ? fallback
    : script.dataset[name] !== "false";

  register({
    project: script.dataset.project || "default",
    environment: script.dataset.environment || "prod",
    endpoint: script.dataset.endpoint || null,
    exporter: script.dataset.exporter || (script.dataset.endpoint ? "http" : "console"),
    capturePrompt: bool("capturePrompt", false),
    captureResponse: bool("captureResponse", false),
    captureArguments: bool("captureArguments", false),
    captureResult: bool("captureResult", false),
    redact: bool("redact", true),
    instrumentFetch: bool("instrumentFetch", true),
    instrumentXHR: bool("instrumentXhr", true),
    instrumentWebSocket: bool("instrumentWebSocket", true),
    debug: bool("debug", false)
  });
}

const api = {
  register,
  unregister,
  flush,
  emit,
  getEvents,
  clearEvents,
  setContext,
  version: VERSION
};

if (typeof globalThis !== "undefined") globalThis.SendaArgus = api;

export {register, unregister, flush, emit, getEvents, clearEvents, setContext};
export default api;

if (typeof document !== "undefined") queueMicrotask(autoRegisterFromScript);
