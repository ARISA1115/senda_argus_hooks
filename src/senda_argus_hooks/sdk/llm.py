from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class MockLLMClient:
    """Tiny SDK-style mock LLM client used by PromptOps tests.

    This class intentionally contains no Argus logging code. Argus logging is
    added by the SDK instrumentor when register(auto_instrument=True) is used.
    """

    provider = "mock"
    model = "mock"

    def generate_answer(self, request: Dict[str, Any]) -> Dict[str, Any]:
        tool_results: Dict[str, Dict[str, Any]] = request.get("tool_results") or {}
        if not tool_results:
            content = "CVE-2024-3094について調査しました。詳細は追加調査が必要です。"
            return {"provider": self.provider, "model": self.model, "content": content}

        vuln = tool_results.get("vulnerability_intelligence", {})
        exploit = tool_results.get("exploit_intelligence", {})
        advisory = tool_results.get("vendor_advisory_lookup", {})

        refs: list[str] = []
        for result in tool_results.values():
            refs.extend(result.get("references", []) or [])
        refs_text = "; ".join(refs) if refs else "未確認です。"
        mitigations = advisory.get("mitigations") or []
        mitigation_text = "; ".join(mitigations) if mitigations else "未確認です。"

        content = f"""# Security Research Result

## summary
{vuln.get('summary', '未確認です。')}

## affected_products
{', '.join(vuln.get('affected_products', [])) or '未確認です。'}

## cvss
{vuln.get('cvss', '未確認です。')}

## exploitation_status
{exploit.get('known_exploitation', '未確認です。')}

## poc_status
{exploit.get('poc_available', '未確認です。')}

## mitigation
{mitigation_text}

## references
{refs_text}

## uncertainty
このMVPではモックMCPを使用しています。実運用では一次情報、ベンダー情報、脅威インテリジェンスで再確認してください。
""".strip()
        return {"provider": self.provider, "model": self.model, "content": content}

    def refine_prompt(self, request: Dict[str, Any]) -> Dict[str, Any]:
        current_prompt = request.get("current_prompt", "")
        missing_capabilities: List[str] = request.get("missing_capabilities") or []
        missing_sections: List[str] = request.get("missing_sections") or []
        required_headings = request.get("required_headings", "")

        additions: list[str] = []
        if missing_capabilities:
            cap = missing_capabilities[0]
            cap_messages = {
                "vulnerability_intelligence": "- vulnerability_intelligence を呼び出して、CVEの概要、影響製品、CVSS、一次参照情報を取得し、内容を解析してください。",
                "exploit_intelligence": "- exploit_intelligence を呼び出して、既知の悪用状況、PoC公開状況、悪用可能性を確認し、内容を解析してください。",
                "vendor_advisory_lookup": "- vendor_advisory_lookup を呼び出して、ベンダーアドバイザリ、修正版、回避策、緩和策を確認し、内容を解析してください。",
            }
            additions.append("必要に応じて、次の調査能力を呼び出して情報を取得し、解析してください。")
            additions.append(cap_messages.get(cap, f"- {cap} を呼び出して、必要な情報を取得し、内容を解析してください。"))

        if missing_sections:
            additions.append("最終回答は必ず Markdown で、次の見出しをこの文字列どおりに含めてください。")
            additions.append(required_headings)
            additions.append("確認できない情報は推測せず、未確認または不明として明記してください。")

        refined = current_prompt.strip()
        if additions:
            refined = refined + "\n\n" + "\n".join(additions).strip()
        return {"provider": self.provider, "model": self.model, "content": refined.strip()}


class OllamaClient:
    """Small Ollama SDK wrapper.

    It performs the actual HTTP call but does not emit Argus events by itself.
    The SDK instrumentor wraps chat() to create llm.request / llm.error events.
    """

    provider = "ollama"

    def __init__(self, *, base_url: str = "http://localhost:11434", model: str = "llama3.1:latest", timeout: int = 120, temperature: float = 0.2):
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def chat(self, messages: List[Dict[str, str]], *, purpose: str = "answer", model: Optional[str] = None, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        selected_model = model or self.model
        request_body: Dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "stream": False,
            "options": options or {"temperature": self.temperature},
        }
        url = self.base_url.rstrip("/") + "/api/chat"
        data = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollamaへの接続に失敗しました: {url}. `ollama serve` が起動しているか確認してください。") from exc
        response = json.loads(raw)
        content: Optional[str] = response.get("message", {}).get("content")
        if not content:
            raise RuntimeError(f"Ollamaから想定外のレスポンスが返りました: {raw[:500]}")
        response.setdefault("provider", self.provider)
        response.setdefault("model", selected_model)
        response.setdefault("purpose", purpose)
        response.setdefault("url", url)
        response.setdefault("request_body", request_body)
        return response
