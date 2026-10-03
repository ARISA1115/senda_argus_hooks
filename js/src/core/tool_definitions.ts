import { createHash } from "node:crypto";
import { foldName, MAX_TOOLS_PER_SERVER } from "./mcp_tools.js";

// MCP の listTools の応答から、ツールごとの定義のダイジェストを作る。
//
// Argus は登録された提供元へ自分で接続して同じ一覧を取得し、エージェントが受け取った定義と突き合わせる。
// 突き合わせはダイジェストの一致で行うため、正規化の規則は Argus の側と一字一句同じでなければならない。
//
// Python の実装 (python/src/senda_argus_hooks/core/tool_definitions.py) と同じ規則を持つ。規則を変えるときは
// 両方を変え、両方のテストが読む共通の例 (tests/fixtures/mcp_tool_definition_hashes.json) を更新する。
//
// 直列化は言語の既定に任せない。鍵はコードポイントの順に並べる。既定の並べ替えは UTF-16 の単位の順で、
// 補助面の文字で Python と順が変わる。数と文字列の表記は JSON.stringify の規則を正とし、Python の側が
// 同じ表記を自前で作る。

function codePoints(key: string): number[] {
  const points: number[] = [];
  for (const ch of key) points.push(ch.codePointAt(0) ?? 0);
  return points;
}

function compareKeys(a: string, b: string): number {
  const pa = codePoints(a);
  const pb = codePoints(b);
  const n = Math.min(pa.length, pb.length);
  for (let i = 0; i < n; i += 1) if (pa[i] !== pb[i]) return pa[i] - pb[i];
  return pa.length - pb.length;
}

export function canonicalJson(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) ? JSON.stringify(value) : "null";
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    // 配列の中の表せない値は JSON.stringify と同じく null にする
    return "[" + value.map((v) => (typeof v === "function" || typeof v === "symbol" ? "null" : canonicalJson(v))).join(",") + "]";
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record)
      .filter((k) => record[k] !== undefined && typeof record[k] !== "function" && typeof record[k] !== "symbol")
      .sort(compareKeys);
    return "{" + keys.map((k) => JSON.stringify(k) + ":" + canonicalJson(record[k])).join(",") + "}";
  }
  throw new TypeError(`not a JSON value: ${typeof value}`);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

export function toolDefinitionHash(tool: any): string | null {
  const name = tool?.name;
  if (typeof name !== "string" || !name) return null;
  const description = tool?.description;
  const schema = tool?.inputSchema;
  const payload = canonicalJson({
    name,
    description: typeof description === "string" ? description : null,
    inputSchema: isPlainObject(schema) ? schema : null
  });
  return "sha256:" + createHash("sha256").update(payload).digest("hex");
}

export function toolDefinitionHashes(response: any): Record<string, string> {
  let tools: unknown = response?.tools;
  if (tools === undefined && Array.isArray(response)) tools = response;
  // 名前は提供元が決める。"__proto__" のような名前で原型を書き換えさせないよう、原型の無い入れ物を使う
  const hashes: Record<string, string> = Object.create(null);
  if (!Array.isArray(tools)) return hashes;
  let count = 0;
  for (const tool of tools) {
    if (count >= MAX_TOOLS_PER_SERVER) break;
    let digest: string | null;
    try {
      digest = toolDefinitionHash(tool);
    } catch {
      // 直列化できない定義は載せない。既定のダイジェストを当てると、別々の定義が同じ値になる
      continue;
    }
    if (digest === null) continue;
    const key = foldName(String((tool as any).name));
    if (!(key in hashes)) count += 1;
    hashes[key] = digest;
  }
  return hashes;
}

// 提供元の URL の正規化。Python の normalize_provider_url と同じ規則で、URL の解析器に任せず自前で行う。
// 解析器ごとに既定のポートや国際化ドメインの扱いが違い、同じ提供元が別の鍵になるため。

const URL_RE = /^([A-Za-z][A-Za-z0-9+.\-]*):\/\/([^/?#]*)([^?#]*)/;
const DEFAULT_PORTS: Record<string, string> = { http: "80", https: "443" };

// RFC 3492 の Punycode の符号化。区間 1 つを受け、xn-- を付けずに返す。
function punycode(label: string): string {
  const base = 36, tMin = 1, tMax = 26, skew = 38, damp = 700;
  const input = codePoints(label);
  let n = 128, delta = 0, bias = 72;
  const output: string[] = input.filter((c) => c < 0x80).map((c) => String.fromCharCode(c));
  const basic = output.length;
  let h = basic;
  if (basic > 0) output.push("-");
  const digit = (d: number) => String.fromCharCode(d + 22 + 75 * (d < 26 ? 1 : 0));
  const adapt = (d: number, numPoints: number, first: boolean) => {
    d = first ? Math.floor(d / damp) : d >> 1;
    d += Math.floor(d / numPoints);
    let k = 0;
    while (d > ((base - tMin) * tMax) >> 1) { d = Math.floor(d / (base - tMin)); k += base; }
    return k + Math.floor(((base - tMin + 1) * d) / (d + skew));
  };
  while (h < input.length) {
    const m = Math.min(...input.filter((c) => c >= n));
    delta += (m - n) * (h + 1);
    n = m;
    for (const c of input) {
      if (c < n) delta += 1;
      if (c === n) {
        let q = delta;
        for (let k = base; ; k += base) {
          const t = k <= bias ? tMin : k >= bias + tMax ? tMax : k - bias;
          if (q < t) break;
          output.push(digit(t + ((q - t) % (base - t))));
          q = Math.floor((q - t) / (base - t));
        }
        output.push(digit(q));
        bias = adapt(delta, h + 1, h === basic);
        delta = 0;
        h += 1;
      }
    }
    delta += 1;
    n += 1;
  }
  return output.join("");
}

function hostLabel(label: string): string {
  return /^[\x00-\x7f]*$/.test(label) ? label : "xn--" + punycode(label);
}

function removeDotSegments(path: string): string {
  const output: string[] = [];
  for (const segment of path.split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") { output.pop(); continue; }
    output.push(segment);
  }
  return "/" + output.join("/");
}

export function normalizeProviderUrl(value: unknown): string | null {
  if (!value) return null;
  const match = URL_RE.exec(String(value).trim());
  if (!match) return null;
  const scheme = match[1].toLowerCase();
  const authority = match[2].slice(match[2].lastIndexOf("@") + 1);
  let host: string;
  let port: string;
  if (authority.startsWith("[")) {
    const end = authority.indexOf("]");
    if (end < 0) return null;
    host = authority.slice(0, end + 1);
    const rest = authority.slice(end + 1);
    if (rest && !rest.startsWith(":")) return null;
    port = rest.slice(1);
  } else {
    const colon = authority.lastIndexOf(":");
    host = colon < 0 ? authority : authority.slice(0, colon);
    port = colon < 0 ? "" : authority.slice(colon + 1);
  }
  if (port && !/^[0-9]+$/.test(port)) return null;
  port = port ? String(BigInt(port)) : "";
  if (port === DEFAULT_PORTS[scheme]) port = "";
  host = host.normalize("NFC").toLowerCase();
  if (!host || host === "[]") return null;
  host = host.split(".").map(hostLabel).join(".");
  const path = removeDotSegments(match[3] || "/").replace(/\/+$/, "") || "/";
  return `${scheme}://${host}${port ? ":" + port : ""}${path}`;
}
