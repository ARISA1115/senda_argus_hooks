export const REDACT_FIELDS: ReadonlySet<string> = new Set([
  "authorization", "api_key", "apikey", "password", "secret", "token",
  "access_token", "refresh_token", "cookie", "set-cookie", "x-api-key"
]);

// 値の形で分かる資格情報。鍵名が一般の項目へ渡されても、平文で通さない。Python の
// DEFAULT_REDACT_PATTERNS と同じ形式をそろえる。bearer だけは接頭辞を残して以降を伏せる。
const patterns: Array<[RegExp, string]> = [
  [/sk-[A-Za-z0-9_-]{12,}/g, "***REDACTED***"],
  [/AKIA[0-9A-Z]{16}/g, "***REDACTED***"],
  [/(bearer\s+)[A-Za-z0-9._-]+/gi, "$1***REDACTED***"],
  [/gh[pousr]_[A-Za-z0-9]{36}/g, "***REDACTED***"],
  [/github_pat_[A-Za-z0-9_]{22,}/g, "***REDACTED***"],
  [/(?:xox[baprse]|xapp)-[A-Za-z0-9-]{10,}/g, "***REDACTED***"],
  [/AIza[0-9A-Za-z_-]{35}/g, "***REDACTED***"],
  [/ya29\.[0-9A-Za-z_-]{20,}/g, "***REDACTED***"],
  [/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, "***REDACTED***"]
];

// 再帰は呼び出しのスタックを使うため、深すぎる入れ子と循環は畳んで目印に置き換える。
const MAX_DEPTH = 100;

// 送出器が JSON にできる形へそろえる。秘匿の置き換えは redactFields のときだけ行う。
export function sanitizeValue(
  value: unknown, redactFields: boolean, ancestors: WeakSet<object> = new WeakSet(), depth = 0
): unknown {
  if (typeof value === "bigint") return value.toString();
  if (value && typeof value === "object") {
    if (ancestors.has(value)) return "[Circular]";
    if (depth >= MAX_DEPTH) return "[MaxDepth]";
    ancestors.add(value);
    try {
      if (Array.isArray(value)) return value.map((item) => sanitizeValue(item, redactFields, ancestors, depth + 1));
      const out: Record<string, unknown> = {};
      for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
        out[key] = redactFields && REDACT_FIELDS.has(key.toLowerCase())
          ? "***REDACTED***"
          : sanitizeValue(item, redactFields, ancestors, depth + 1);
      }
      return out;
    } finally {
      ancestors.delete(value);
    }
  }
  if (redactFields && typeof value === "string") {
    return patterns.reduce((current, [pattern, replacement]) => current.replace(pattern, replacement), value);
  }
  return value;
}

export function redactValue(value: unknown): unknown {
  return sanitizeValue(value, true);
}

export function sanitizeEvent<T>(event: T, redactFields: boolean): T {
  const sanitized = sanitizeValue(event, redactFields) as any;
  if (redactFields) sanitized.security = { ...(sanitized.security ?? {}), redacted: true };
  return sanitized as T;
}

export function redactEvent<T>(event: T): T {
  return sanitizeEvent(event, true);
}
