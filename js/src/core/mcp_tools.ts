import { createHash } from "node:crypto";

// MCP のツール一覧を観測して、LLM に差し出したツールの名前からサーバを引く台帳。
//
// Python の実装 (python/src/senda_argus_hooks/core/mcp_tools.py) と同じ規則を持つ。規則を変えるときは
// 両方を変え、両方の試験が読む共通の例 (tests/fixtures/mcp_tool_attribution.json) を足す。
//
// 引けないときは何も載せない。同じ名前が 2 つのサーバに在る、どのサーバにも無い、といった場合に
// どれかを既定として当てると、信頼したサーバの帰属を別のツールへ付けることになり、判定が逆になる。
// 名前の突き合わせは、名前がそのまま一覧に在るか、<server>__<tool> か <server>_<tool> の形の 2 通りだけ。

export const UNNAMED_MCP_SERVER = "unknown";
export const MAX_SERVERS = 64;
export const MAX_TOOLS_PER_SERVER = 512;
// これより長い名前は SHA-256 に畳んで控え、引くときも同じく畳んでから比べる。捨てると、長い名前を
// 選ぶだけでそのツールの帰属が引けなくなる。
export const MAX_NAME_LEN = 256;
const SEPARATORS = ["__", "_"] as const;

export function foldName(name: string): string {
  // Python の len と揃えるため、UTF-16 の単位ではなくコードポイントで数える。
  if ([...name].length <= MAX_NAME_LEN) return name;
  return "sha256:" + createHash("sha256").update(name).digest("hex");
}

export type OfferedAlternative = { name: string; mcp_server?: string };

export class McpToolDirectory {
  private readonly servers = new Map<string, Set<string>>();
  // 名前ごとに、その名前でツールを控えたクライアント。同じ名前を 2 つの生きたクライアントが使ったら
  // 衝突として扱い、以後どのツールにもその名前を付けない。前のクライアントが消えた後に別の
  // クライアントが同じ名前を使ったら、控えを捨ててから引き継ぐ。Python の実装と同じ規則。
  private readonly owners = new Map<string, WeakRef<object>>();
  private readonly conflicted = new Set<string>();

  constructor(private readonly maxServers = MAX_SERVERS, private readonly maxTools = MAX_TOOLS_PER_SERVER) {}

  // 一覧の 1 回分を控える。一覧は頁に分かれて届くことがあるため、前に控えた名前へ足す。
  // 名前を持たないセッションは控えない。
  record(server: unknown, toolNames: Iterable<unknown>, client?: object): void {
    const serverName = typeof server === "string" ? server.trim() : "";
    if (!serverName || serverName === UNNAMED_MCP_SERVER || this.conflicted.has(serverName)) return;
    const names: string[] = [];
    for (const n of toolNames) if (typeof n === "string" && n) names.push(foldName(n));
    if (!names.length) return;
    if (client !== undefined && !this.claim(serverName, client)) return;
    if (this.conflicted.has(serverName)) return;
    const known = this.servers.get(serverName) ?? new Set<string>();
    this.servers.delete(serverName);
    for (const name of names) {
      if (known.has(name)) continue;
      if (known.size >= this.maxTools) break;
      known.add(name);
    }
    this.servers.set(serverName, known);
    while (this.servers.size > this.maxServers) {
      const oldest = this.servers.keys().next().value;
      if (oldest === undefined) break;
      this.servers.delete(oldest);
      this.owners.delete(oldest);
    }
  }

  claim(server: string, client: object): boolean {
    if (this.conflicted.has(server)) return false;
    const owner = this.owners.get(server)?.deref();
    if (owner === client) return true;
    if (owner !== undefined) {
      this.conflicted.add(server);
      this.servers.delete(server);
      this.owners.delete(server);
      return false;
    }
    this.servers.delete(server);
    this.owners.set(server, new WeakRef(client));
    while (this.owners.size > this.maxServers) {
      const oldest = this.owners.keys().next().value;
      if (oldest === undefined) break;
      this.owners.delete(oldest);
    }
    return true;
  }

  isConflicted(server: string): boolean { return this.conflicted.has(server); }

  // 差し出した名前のサーバを返す。1 つに決まらなければ空文字を返す。
  serverOf(name: unknown): string {
    if (typeof name !== "string" || !name) return "";
    const folded = foldName(name);
    const exact = new Set<string>();
    for (const [server, tools] of this.servers) if (tools.has(folded)) exact.add(server);
    if (exact.size) return exact.size === 1 ? [...exact][0] : "";
    const prefixed = new Set<string>();
    for (const [server, tools] of this.servers) {
      for (const sep of SEPARATORS) {
        const head = server + sep;
        if (name.startsWith(head) && tools.has(foldName(name.slice(head.length)))) prefixed.add(server);
      }
    }
    return prefixed.size === 1 ? [...prefixed][0] : "";
  }

  alternatives(offered: Iterable<string>): OfferedAlternative[] {
    const out: OfferedAlternative[] = [];
    for (const name of offered) {
      const server = this.serverOf(name);
      out.push(server ? { name, mcp_server: server } : { name });
    }
    return out;
  }

  clear(): void { this.servers.clear(); this.owners.clear(); this.conflicted.clear(); }
}

const directory = new McpToolDirectory();

export function getMcpToolDirectory(): McpToolDirectory { return directory; }

// 計装が候補を送るときの唯一の組み立て。
export function offeredAlternatives(offered: Iterable<string>): OfferedAlternative[] {
  return directory.alternatives(offered);
}

// listTools の応答からツールの名前を取り出す。
export function toolNamesOf(response: any): string[] {
  const tools = Array.isArray(response) ? response : response?.tools;
  if (!Array.isArray(tools)) return [];
  const names: string[] = [];
  for (const tool of tools) {
    const name = tool?.name;
    if (typeof name === "string" && name) names.push(name);
  }
  return names;
}
