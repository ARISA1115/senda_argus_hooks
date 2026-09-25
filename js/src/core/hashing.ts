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

// JSON.stringify は undefined や関数に対して文字列でなく undefined を返すため、必ず文字列へそろえる。
export function stableStringify(value: unknown): string {
  try {
    const text = JSON.stringify(normalize(value));
    if (typeof text === "string") return text;
  } catch {
    // 循環や深すぎる入れ子は下の String へ落とす
  }
  try { return String(value); }
  catch { return "[unserializable]"; }
}

export function sha256Value(value: unknown): string {
  return createHash("sha256").update(stableStringify(value)).digest("hex");
}

export function stableHash(value: unknown, prefix: string, length = 16): string {
  return `${prefix}_${sha256Value(value).slice(0, length)}`;
}
