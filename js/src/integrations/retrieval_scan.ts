import { getConfig } from "../runtime.js";
import { retrievedDocumentsScanFields } from "../core/result_scan.js";

// 取得した文書の 1 件から本文を読む。node は LlamaIndex、pageContent は LangChain の形である。
function documentText(item: any): string | undefined {
  const node = item?.node ?? item;
  if (typeof node === "string") return node;
  if (typeof node?.getContent === "function") {
    try {
      const text = node.getContent();
      if (typeof text === "string") return text;
    } catch {
      // 読めない文書は走査の文に含めない。
    }
  }
  for (const value of [node?.text, node?.pageContent]) {
    if (typeof value === "string") return value;
  }
  return undefined;
}

// ハッシュと件数だけでは、文書に埋め込まれた指示が注入の規則に届かない。tool の戻り値と同じ秘匿と上限を通す。
export function retrievedTextScanFields(result: any): Record<string, unknown> {
  if (!getConfig().scanResult) return {};
  const items = Array.isArray(result) ? result : result?.sourceNodes ?? result?.source_nodes ?? [];
  if (!Array.isArray(items)) return {};
  const texts = items.map(documentText).filter((text): text is string => typeof text === "string" && text.trim() !== "");
  return texts.length ? { ...retrievedDocumentsScanFields(texts) } : {};
}
