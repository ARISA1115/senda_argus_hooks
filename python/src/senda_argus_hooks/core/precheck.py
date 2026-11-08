"""ツールの実行の前に Argus の判定を問い合わせ、拒否と承認待ちの呼び出しを止める。

既定で無効である。主体を結び付けていない鍵では全ての呼び出しが最も厳しい段階で拒否になるため、導入する側が
`enforce_precheck` で有効にする。引数は秘匿した走査の文だけを送る。
接続できないときは `precheck_fail_open` に従う。既定は通す。
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.result_scan import (
    ARGUMENTS_FAILED_FIELD,
    ARGUMENTS_SCAN_FIELD,
    ARGUMENTS_TRUNCATED_FIELD,
    arguments_scan_fields,
)
from senda_argus_hooks.core.runtime import effective_agent_id, get_config

logger = logging.getLogger("senda_argus_hooks.precheck")

PRECHECK_PATH = "/v1/hooks/precheck"
PRODUCT = "sdk"
TIMEOUT_SEC = 5.0
_READ_LIMIT = 65536
_ALLOW = "allow"
_STOP = frozenset({"deny", "pending"})

_endpoint: str | None = None
_api_key: str = ""


class ToolCallBlocked(RuntimeError):
    """Argus の判定により、ツールを実行しなかったことを示す。"""

    def __init__(self, decision: str, reason: str, tool: str | None = None) -> None:
        self.decision = decision
        self.reason = reason
        self.tool = tool
        super().__init__(f"Argus がツールの実行を止めました decision={decision} tool={tool} reason={reason}")


def configure_precheck(endpoint: str | None, api_key: str = "") -> None:
    global _endpoint, _api_key
    _endpoint = endpoint.rstrip("/") if endpoint else None
    _api_key = api_key


def precheck_enabled() -> bool:
    return bool(_endpoint) and bool(get_config().enforce_precheck)


def reset_precheck() -> None:
    """登録を解いた後に、前の宛先へ問い合わせて呼び出しを止めないよう、宛先を消す。"""
    configure_precheck(None)


def _tool_input(arguments: Any) -> dict[str, Any]:
    """判定へ送る引数。scan_arguments に関わらず秘匿した文を送る。送らないと内容を判定できず、承認も引数で区別できない。

    全文のダイジェストを添える。切り詰めで中央だけが違う呼び出しの承認を、別の呼び出しへ流用させない。
    切り詰めと作成の失敗は判定へ伝え、受け取り側は読めない呼び出しとして止める。
    """
    fields = arguments_scan_fields(arguments)
    out: dict[str, Any] = {}
    try:
        out["arguments_sha256"] = sha256_value(arguments)
    except (TypeError, ValueError):
        # 孤立サロゲートなど、ダイジェストを作れない引数は読めない呼び出しとして判定へ伝える。
        out["arguments_unreadable"] = True
    text = fields.get(ARGUMENTS_SCAN_FIELD)
    if text:
        out["arguments"] = text
    if fields.get(ARGUMENTS_TRUNCATED_FIELD) or fields.get(ARGUMENTS_FAILED_FIELD):
        out["arguments_unreadable"] = True
    return out


def _request_body(tool_name: str, arguments: Any, mcp_server: str | None) -> dict[str, Any]:
    cfg = get_config()
    body: dict[str, Any] = {
        "product": PRODUCT,
        "agent_id": effective_agent_id({"sdk": PRODUCT}),
        "session_id": cfg.session_id or cfg.run_id or cfg.project,
        "tool_name": tool_name,
        "tool_input": _tool_input(arguments),
    }
    if mcp_server:
        body["mcp_server"] = mcp_server
    return body


def _ask(body: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if _api_key:
        headers["X-API-Key"] = _api_key
    request = urllib.request.Request(  # noqa: S310 - 送出器と同じ宛先である
        f"{_endpoint}{PRECHECK_PATH}",
        # ensure_ascii で符号化の失敗を無くす。孤立サロゲートの 1 文字で判定を外させない。
        data=json.dumps(body, ensure_ascii=True, default=str).encode("ascii"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:  # noqa: S310
        parsed = json.loads(response.read(_READ_LIMIT).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("the response is not an object")
    return parsed


def _fail_open(name: str, exc: Exception) -> None:
    logger.warning("precheck_unavailable: %s", exc.__class__.__name__)
    if not get_config().precheck_fail_open:
        raise ToolCallBlocked("unavailable", "the pre-execution check is unavailable", name) from exc


def check_tool_call(tool_name: str | None, arguments: Any, *, mcp_server: str | None = None) -> None:
    """判定を問い合わせる。止めるときは ToolCallBlocked を投げる。無効なら何もしない。"""
    if not precheck_enabled():
        return
    name = str(tool_name or "unknown")
    body = _request_body(name, arguments, mcp_server)
    try:
        verdict = _ask(body)
        decision = verdict.get("decision")
        if decision != _ALLOW and decision not in _STOP:
            raise ValueError("unknown decision")
    except urllib.error.HTTPError as exc:
        # 4xx は判定の拒否で、接続の問題ではない。失効した鍵や端末の検証の失敗で実行させない。
        if exc.code < 500:
            raise ToolCallBlocked("rejected", f"the check returned {exc.code}", name) from exc
        _fail_open(name, exc)
        return
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        _fail_open(name, exc)
        return
    if decision in _STOP:
        raise ToolCallBlocked(str(decision), str(verdict.get("reason") or ""), name)
