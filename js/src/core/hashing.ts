import { createHash } from "node:crypto";

function normalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalize);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      const v = (value as Record<string, unknown>)[key];
      if (typeof v !== "function" && typeof v !== "symbol") out[key] = normalize(v);
    }
    return out;
  }
  if (typeof value === "bigint") return value.toString();
  return value;
}

export function stableStringify(value: unknown): string {
  try { return JSON.stringify(normalize(value)); }
  catch { return String(value); }
}

export function sha256Value(value: unknown): string {
  return createHash("sha256").update(stableStringify(value)).digest("hex");
}

export function stableHash(value: unknown, prefix: string, length = 16): string {
  return `${prefix}_${sha256Value(value).slice(0, length)}`;
}
