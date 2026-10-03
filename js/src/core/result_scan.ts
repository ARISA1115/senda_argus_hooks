import { redactValue } from "./redaction.js";

// tool の戻り値から、検知の走査だけに使う文を作る。
//
// 戻り値の本文は captureResult を有効にしたときだけ送る。本文を送らない既定の導入では、戻り値に
// 埋め込まれた指示が Argus の注入の規則に一度も届かない。ここで作る文は走査のためだけに送り、
// Argus は保存せず、判定へ渡した後に捨てる。本文の保存とは別の設定で切り替える。
//
// 秘匿は平坦にする前に当てる。鍵名で資格情報と分かる項目は、平坦にすると鍵名との対応が失われる。
// 長さには上限を置き、超えた文は先頭と末尾を残して中央を落とす。先頭だけを残すと、長い前置きの
// 後ろへ指示を置くだけで走査から外せる。値は Python の計装と揃える。

export const RESULT_SCAN_MAX_CHARS = 32768;
export const RESULT_SCAN_ELISION = "\n...\n";
const MAX_DEPTH = 32;

function collect(value: unknown, out: string[], depth: number, cut: { hit: boolean }): void {
  if (value !== null && typeof value === "object" && depth >= MAX_DEPTH) { cut.hit = true; return; }
  if (typeof value === "string" && (value === "[MaxDepth]" || value === "[Circular]")) { cut.hit = true; return; }
  if (typeof value === "string") {
    out.push(value);
  } else if (Array.isArray(value)) {
    for (const item of value) collect(item, out, depth + 1, cut);
  } else if (value && typeof value === "object") {
    // Argus の走査も辞書の鍵を読む。鍵を落とすと、構造化した戻り値の鍵へ置いた指示が届かない。
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      out.push(key);
      collect(item, out, depth + 1, cut);
    }
  }
}

export interface ResultScanFields {
  result_scan?: string;
  result_scan_truncated?: boolean;
  result_scan_length?: number;
  result_scan_failed?: boolean;
}

// 事象へ載せる走査の文と印。切り詰めたら印と元の長さを載せる。作成に失敗しても例外を出さず、
// 落としたことを示す印だけを返す。事象は送る。
export function resultScanFields(value: unknown): ResultScanFields {
  try {
    const parts: string[] = [];
    const cut = { hit: false };
    collect(redactValue(value), parts, 0, cut);
    const kept = parts.filter((part) => part.trim() !== "");
    if (kept.length === 0) return cut.hit ? { result_scan_truncated: true } : {};
    let text = kept.join(" ");
    const out: ResultScanFields = {};
    if (text.length > RESULT_SCAN_MAX_CHARS) {
      out.result_scan_truncated = true;
      out.result_scan_length = text.length;
      const keep = Math.floor((RESULT_SCAN_MAX_CHARS - RESULT_SCAN_ELISION.length) / 2);
      text = text.slice(0, keep) + RESULT_SCAN_ELISION + text.slice(text.length - keep);
    } else if (cut.hit) {
      out.result_scan_truncated = true;
    }
    out.result_scan = text;
    return out;
  } catch {
    return { result_scan_failed: true };
  }
}

export function resultScanText(value: unknown): string | undefined {
  return resultScanFields(value).result_scan;
}
