const fields = new Set([
  "authorization", "api_key", "apikey", "password", "secret", "token",
  "access_token", "refresh_token", "cookie", "set-cookie", "x-api-key"
]);

const patterns: Array<[RegExp, string]> = [
  [/sk-[A-Za-z0-9_-]{12,}/g, "***REDACTED***"],
  [/AKIA[0-9A-Z]{16}/g, "***REDACTED***"],
  [/(bearer\s+)[A-Za-z0-9._-]+/gi, "$1***REDACTED***"]
];

// 再帰は呼び出しのスタックを使うため、深すぎる入れ子と循環は畳んで目印に置き換える。
const MAX_DEPTH = 100;

export function redactValue(value: unknown, ancestors: WeakSet<object> = new WeakSet(), depth = 0): unknown {
  if (value && typeof value === "object") {
    if (ancestors.has(value)) return "[Circular]";
    if (depth >= MAX_DEPTH) return "[MaxDepth]";
    ancestors.add(value);
    try {
      if (Array.isArray(value)) return value.map((item) => redactValue(item, ancestors, depth + 1));
      const out: Record<string, unknown> = {};
      for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
        out[key] = fields.has(key.toLowerCase()) ? "***REDACTED***" : redactValue(item, ancestors, depth + 1);
      }
      return out;
    } finally {
      ancestors.delete(value);
    }
  }
  if (typeof value === "string") {
    return patterns.reduce((current, [pattern, replacement]) => current.replace(pattern, replacement), value);
  }
  return value;
}

export function redactEvent<T>(event: T): T {
  const redacted = redactValue(event) as any;
  redacted.security = { ...(redacted.security ?? {}), redacted: true };
  return redacted as T;
}
