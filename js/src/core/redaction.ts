const fields = new Set([
  "authorization", "api_key", "apikey", "password", "secret", "token",
  "access_token", "refresh_token", "cookie", "set-cookie", "x-api-key"
]);

const patterns: Array<[RegExp, string]> = [
  [/sk-[A-Za-z0-9_-]{12,}/g, "***REDACTED***"],
  [/AKIA[0-9A-Z]{16}/g, "***REDACTED***"],
  [/(bearer\s+)[A-Za-z0-9._-]+/gi, "$1***REDACTED***"]
];

export function redactValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactValue);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      out[key] = fields.has(key.toLowerCase()) ? "***REDACTED***" : redactValue(item);
    }
    return out;
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
