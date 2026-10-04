from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.identity import (
    SERVER_INFO_NAME_ATTR,
    UNNAMED_MCP_SERVER,
    data_source_hash,
    derive_mcp_profile_id,
    derive_purpose_id,
    mcp_data_source_profile,
    normalize_url,
    resolve_mcp_server_name,
    resolve_mcp_server_url,
)
from senda_argus_hooks.core.acquisitions import acquisitions_with_overflow
from senda_argus_hooks.core.external_content import get_external_content_ledger, local_instruction_digests
from senda_argus_hooks.core.instruction_files import classify_instruction_write, instruction_file_name, instruction_file_path
from senda_argus_hooks.core.mcp_tools import (
    MAX_TOOLS_PER_SERVER,
    get_mcp_tool_directory,
    read_only_tool_names_of,
    tool_names_of,
)
from senda_argus_hooks.core.egress_hosts import egress_hosts_with_overflow
from senda_argus_hooks.core.monitor_targets import monitor_targets
from senda_argus_hooks.core.resource_access import (
    classify_read_resource,
    classify_resource_access,
)
from senda_argus_hooks.core.result_scan import result_scan_fields, scan_source
from senda_argus_hooks.core.runtime import effective_agent_id, emit_event, get_config
from senda_argus_hooks.core.tool_definitions import (
    normalize_provider_url,
    tool_definition_hashes,
)
from senda_argus_hooks.core.tool_result import tool_result_is_error

from .base import BaseInstrumentor, audit_guard

_LOGGER = logging.getLogger(__name__)


class MCPPythonInstrumentor(BaseInstrumentor):
    name = "mcp_python"

    def __init__(self):
        self._patches: list[tuple[Any, str, Callable]] = []

    def instrument(self) -> bool:
        candidates = []
        with contextlib.suppress(Exception):
            from mcp import ClientSession
            candidates.append((ClientSession, "call_tool", "call_tool"))
            candidates.append((ClientSession, "read_resource", "read_resource"))
            candidates.append((ClientSession, "list_tools", "list_tools"))
            candidates.append((ClientSession, "list_resources", "list_resources"))
            # 提供元が返すプロンプトの本文も外部から来た内容である。指示ファイルの出所の印のために控える。
            candidates.append((ClientSession, "get_prompt", "get_prompt"))
            # 初期化は事象を出さない。応答が名乗るサーバ名をセッションへ控えるだけにする。
            candidates.append((ClientSession, "initialize", "initialize"))
        patched = False
        for cls, method_name, op in candidates:
            original = getattr(cls, method_name, None)
            if original is None or hasattr(original, "__senda_patched__"):
                continue
            wrapped = self._wrap_initialize(original) if op == "initialize" else self._wrap(original, op)
            wrapped.__senda_patched__ = True
            setattr(cls, method_name, wrapped)
            self._patches.append((cls, method_name, original))
            patched = True
        return patched

    def _wrap(self, original: Callable, operation: str) -> Callable:
        def sync_wrapper(obj, *args, **kwargs):
            result = original(obj, *args, **kwargs)
            if hasattr(result, "__await__"):
                async def awaited():
                    return await self._observe_async_call(original_result=result, operation=operation, obj=obj, args=args, kwargs=kwargs)
                return awaited()
            return result
        return sync_wrapper

    @staticmethod
    def _wrap_initialize(original: Callable) -> Callable:
        def sync_wrapper(obj, *args, **kwargs):
            result = original(obj, *args, **kwargs)
            if not hasattr(result, "__await__"):
                return result

            async def awaited():
                response = await result
                with audit_guard("initialize"):
                    _remember_server_info_name(obj, response)
                return response

            return awaited()

        return sync_wrapper

    async def _observe_async_call(self, *, original_result, operation: str, obj, args, kwargs):
        cfg = get_config()
        started = time.perf_counter()
        meta = _mcp_metadata(obj, operation, args, kwargs)
        purpose_id = meta["purpose_id"]
        if operation == "call_tool":
            emit_event(
                "mcp.tool_call.requested",
                source={"component": "instrumentor", "sdk": "mcp_python", "operation": operation},
                data={"mcp": meta},
                status="start",
                purpose_id=purpose_id,
            )
        try:
            response = await original_result
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            emit_event(
                "mcp.tool_call.failed" if operation == "call_tool" else f"mcp.{operation}.failed",
                source={"component": "instrumentor", "sdk": "mcp_python", "operation": operation},
                data={"mcp": meta},
                status="error",
                latency_ms=latency_ms,
                error={"type": exc.__class__.__name__, "message": str(exc)},
                purpose_id=purpose_id,
            )
            raise

        # 観測の後処理は本来の呼び出しから隔離する。ここで失敗しても応答は返す。
        with audit_guard(operation):
            latency_ms = int((time.perf_counter() - started) * 1000)
            result_payload = _safe_response(response)
            data = {"mcp": {**meta}}
            if cfg.capture_result:
                data["mcp"]["result"] = result_payload
            data["mcp"]["result_hash"] = sha256_value(result_payload)
            if operation == "call_tool":
                # 結果の本文を送らない設定でも、エラーの印だけは常に送る。判別できない形なら載せない。
                is_error = tool_result_is_error(response)
                if is_error is not None:
                    data["mcp"]["is_error"] = is_error
            if operation in ("call_tool", "read_resource", "get_prompt"):
                # 提供元から受け取った内容を控える。エラーの印が立った応答も本文はモデルへ渡るため控える。
                # 指示ファイルの読み取りでは、手元に実在するそのファイルの今の中身にも在る行だけを控えない。
                # 読んで書き戻すたびに、既に在った行が外部から来たことになるため。名前だけで除外すると、
                # 提供元が資源や引数に指示ファイルの名前を付けるだけで控えを止められる。
                if operation == "call_tool":
                    target = instruction_file_path(arguments_of_call(args, kwargs))
                elif operation == "read_resource":
                    target = str(args[0] if args else kwargs.get("uri") or "")
                else:
                    target = None
                if target and instruction_file_name(target.split("?", 1)[0]) is None:
                    target = None
                get_external_content_ledger().record(
                    effective_agent_id(), scan_source(response), exclude=local_instruction_digests(target)
                )
            if operation == "call_tool" and cfg.scan_result:
                # 本文を送らない既定でも、戻り値に埋め込まれた指示が注入の規則に届くようにする。
                data["mcp"].update(result_scan_fields(scan_source(response)))
            if operation == "list_tools":
                # 一覧に出たツールをサーバごとに控える。LLM に差し出した候補のサーバはここから引く。
                get_mcp_tool_directory().record(meta["server"], tool_names_of(response), session=obj)
                # 読み取りだけと宣言したツールを控える。本文を持たない呼び出しの向きを決めるのに使う。
                with contextlib.suppress(Exception):
                    _record_read_only_tools(obj, response, continuation=_is_continuation_page(args, kwargs))
                # 受け取った定義のダイジェストを載せる。Argus は提供元へ自分で取得した定義と突き合わせ、
                # 呼び出し元によって定義を変える提供元を捉える。本文は載せない。
                hashes = tool_definition_hashes(response)
                if hashes:
                    data["mcp"]["tool_definition_hashes"] = hashes
                    # 突き合わせの鍵は提供元の正規化で作る。URL に含まれる資格情報を送らず、既定のポートや
                    # 区切りの違いで Argus の取得と別の鍵にならないようにする。
                    data["mcp"]["server_url"] = normalize_provider_url(resolve_mcp_server_url(obj))
            emit_event(
                _completed_event_type(operation),
                source={"component": "instrumentor", "sdk": "mcp_python", "operation": operation},
                data=data,
                status="success",
                latency_ms=latency_ms,
                purpose_id=purpose_id,
            )
        return response

    def uninstrument(self) -> bool:
        for cls, method_name, original in self._patches:
            setattr(cls, method_name, original)
        self._patches = []
        return True


# 一覧の取得の完了。受け取った定義のダイジェストを運び、Argus はこの種別を判定の入口に数える。
# 種別の名前を合成せずに置き、受け取り側の表と文字列で突き合わせられるようにする。
LIST_TOOLS_COMPLETED = "mcp.list_tools.completed"


# 資源の直接読み取りの完了。Argus は資源の往復と主体の間の連絡路で、読み取りの側をこの種別で受ける。
READ_RESOURCE_COMPLETED = "mcp.read_resource.completed"

# 提供元が読み取りだけと宣言したツールの名前を、セッションへ控える属性。
READ_ONLY_TOOLS_ATTR = "_senda_argus_read_only_tools"


def _is_continuation_page(args, kwargs) -> bool:
    """一覧の取得が続きの頁か。継続位置を渡した取得を続きの頁とする。

    list_tools は継続位置を位置引数、cursor、params.cursor のいずれかで受ける。
    """
    cursor = kwargs.get("cursor")
    if cursor is None and args:
        first = args[0]
        cursor = first if isinstance(first, str) else getattr(first, "cursor", None)
    params = kwargs.get("params")
    if cursor is None and params is not None:
        cursor = params.get("cursor") if isinstance(params, dict) else getattr(params, "cursor", None)
    return cursor is not None


def _record_read_only_tools(obj, response, *, continuation: bool) -> None:
    """読み取りだけと宣言したツールの名前を控える。

    続きの頁は前の頁の控えへ足し、継続位置の無い取得、つまり新しい一覧の始まりでだけ空にする。
    続きの頁で置き換えると、前の頁で宣言したツールの向きが引けなくなる。続きの頁に読み取りだけと
    宣言せずに出た名前は控えから外す。

    控えはサーバごとのツールの上限と同じ数で打ち切る。提供元は続きの頁を返すたびに名前を足せる。
    上限の外の名前は読み取りとして扱わず、向きを載せない。落とした数は記録に残す。
    """
    declared = read_only_tool_names_of(response)
    listed = {n for n in tool_names_of(response) if isinstance(n, str)}
    known = set(getattr(obj, READ_ONLY_TOOLS_ATTR, None) or ()) if continuation else set()
    known -= listed - set(declared)
    dropped = 0
    for name in declared:
        if name in known:
            continue
        if len(known) >= MAX_TOOLS_PER_SERVER:
            dropped += 1
            continue
        known.add(name)
    if dropped:
        _LOGGER.warning("senda_argus_read_only_tools_dropped count=%d", dropped)
    setattr(obj, READ_ONLY_TOOLS_ATTR, frozenset(known))


def _completed_event_type(operation: str) -> str:
    if operation == "call_tool":
        return "mcp.tool_call.completed"
    if operation == "list_tools":
        return LIST_TOOLS_COMPLETED
    if operation == "read_resource":
        return READ_RESOURCE_COMPLETED
    return f"mcp.{operation}.completed"


def _session_server_name(obj: Any) -> Any:
    """セッションのサーバ名。名乗りから採った名前が別のセッションと衝突したら、名前を持たないものとして扱う。

    明示の名前は利用者の設定で、同じ名前の複数のセッションを持つ構成もあるため、呼び出しの記録には
    そのまま残す。帰属の台帳は明示の名前も持ち主で絞る。
    """
    name = resolve_mcp_server_name(obj)
    if name == getattr(obj, SERVER_INFO_NAME_ATTR, None) and not get_mcp_tool_directory().claim(name, obj):
        return UNNAMED_MCP_SERVER
    return name


def _remember_server_info_name(obj: Any, response: Any) -> None:
    """初期化の応答が名乗ったサーバ名を、セッションへ控える。

    SDK のセッションはこの名前を保持しないため、控えないとサーバ名は予約した名前になり、Argus は
    そのサーバを承認できず、一覧のツールの帰属も引けない。明示の名前を持つセッションでは読み方の
    順で明示の名前が勝つため、控えても結果は変わらない。
    """
    info = getattr(response, "serverInfo", None)
    if info is None and isinstance(response, dict):
        info = response.get("serverInfo")
    name = info.get("name") if isinstance(info, dict) else getattr(info, "name", None)
    # 名乗りはサーバが決める値である。同じ名前を別のセッションが既に名乗っていれば衝突として控えず、
    # 候補にも呼び出しにもその名前を付けない。承認済みのサーバの名前を名乗った偽のサーバを、
    # 承認済みのものとして送らない。
    if (
        isinstance(name, str)
        and name.strip()
        and get_mcp_tool_directory().claim(name.strip(), obj)
    ):
        setattr(obj, SERVER_INFO_NAME_ATTR, name.strip())


def arguments_of_call(args, kwargs) -> Any:
    """call_tool の引数の辞書を返す。"""
    return _extract_arguments("call_tool", args, kwargs).get("arguments")


def _extract_arguments(operation: str, args, kwargs) -> dict[str, Any]:
    if operation == "call_tool":
        return {"tool": args[0] if args else kwargs.get("name"), "arguments": args[1] if len(args) > 1 else kwargs.get("arguments")}
    return {"args": args, "kwargs": kwargs}


def _mcp_metadata(obj, operation: str, args, kwargs) -> dict[str, Any]:
    cfg = get_config()
    arguments = _extract_arguments(operation, args, kwargs)
    tool_name = arguments.get("tool")
    server_name = _session_server_name(obj)
    server_url = resolve_mcp_server_url(obj)
    capability = kwargs.get("capability") or getattr(obj, "capability", None)
    args_hash = sha256_value(arguments)
    mcp_profile_id = derive_mcp_profile_id(mcp_server_name=server_name, mcp_server_url=server_url)
    purpose_profile = mcp_data_source_profile(mcp_server_name=server_name, mcp_server_url=server_url, tool_name=str(tool_name), capability=capability)
    purpose_id = derive_purpose_id(mcp_server_name=server_name, mcp_server_url=server_url, tool_name=str(tool_name), capability=capability)
    source_hash = data_source_hash(purpose_profile)
    meta = {
        "operation": operation,
        "server": server_name,
        "server_url": normalize_url(str(server_url)) if server_url else None,
        "tool": tool_name,
        "capability": capability,
        "mcp_profile_id": mcp_profile_id,
        "purpose_id": purpose_id,
        "purpose_source": "mcp_data_source_hash",
        "purpose_profile": purpose_profile,
        "data_source_hash": source_hash,
        # 呼び出しを包んだ外側の辞書のダイジェスト。相関の追跡に使う既存の項目であり、
        # 判定が読む書き込みの内容のダイジェストは分類器が資源の引数から作って上書きする。
        "arguments_hash": args_hash,
    }
    # 提供元の識別には、表示名だけでなく正規化した URL も含める。表示名は重複しうるし、
    # 取得できないときは同じ既定値へ落ちる。名前だけで括ると、別の提供元の同名の資源が
    # 同じ鍵に集まり、無関係な読み書きが往復に見える。
    resource_scope = mcp_profile_id
    if operation == "call_tool":
        # 同じ資源への読み取りと書き込みを 1 つの鍵で結び付ける。data_source_hash はツール名を
        # 含むため、同じ資源でも読み取りと書き込みで別の値になり、往復を追う鍵にならない。
        # 名前そのものは載せない。判定に要るのは同一性だけで、名前を運ぶと受け取り側の権威記録に残る。
        read_only = str(tool_name) in (getattr(obj, READ_ONLY_TOOLS_ATTR, None) or ())
        meta.update(
            classify_resource_access(arguments.get("arguments"), server=resource_scope, read_only=read_only)
        )
        # 宛先と監視の構成要素の区分。引数の本文を送らない設定でも、受け取り側の判定に要る正規化した
        # 値だけを送る。導出は受け取り側と同じ規則である。
        hosts, truncated = egress_hosts_with_overflow(arguments.get("arguments"))
        if hosts:
            meta["egress_hosts"] = hosts
        if truncated:
            meta["egress_hosts_truncated"] = True
        targets = monitor_targets(arguments.get("arguments"), tool=tool_name)
        if targets:
            meta["monitor_targets"] = targets
        # 指示ファイルへの書き込みは、次のエージェントへ払い出しが渡る経路になる。突合に使う
        # ダイジェストだけを載せる。本文は載せない。分類の可否は受け取り側が名前から判定し直すため、
        # ここでの分類は候補の提示にとどまる。
        written = classify_instruction_write(arguments.get("arguments"))
        if written:
            meta.update(written)
            # 書き込みのダイジェストのうち、提供元から受け取った内容に在ったもの。受け取り側は同じ主体の
            # 指示に現れたこれを記憶の汚染として扱う。本文は載せない。
            meta.update(get_external_content_ledger().classify(effective_agent_id(), written))
        # 依存の導入と資源の取得の取得先。受け取り側と同じ規則で導き、名前と版と指定されたダイジェストを
        # 記録に残す。判定は取得先の並びだけを読む。
        acquisitions, acquisitions_truncated = acquisitions_with_overflow(arguments.get("arguments"))
        if acquisitions:
            meta["acquisitions"] = acquisitions
            meta["acquisition_sources"] = [item["source"] for item in acquisitions]
        if acquisitions_truncated:
            meta["acquisition_sources_truncated"] = True
    elif operation == "read_resource":
        # 資源の直接読み取りは引数の形が違い、位置引数か uri に資源が直接入る。ここを通さないと
        # 同じ資源への読み取りがツール呼び出しの書き込みと結び付かず、往復として現れない。
        # 指示ファイルの分類は本文にあたる引数を要するため、この経路では該当しない。
        #
        # 分岐は当該の操作に限る。一覧を取る操作も引数を位置で受けるため、まとめて通すと
        # 継続位置の値を資源と取り違え、存在しない鍵を読み取りとして作ってしまう。
        meta.update(
            classify_read_resource(arguments.get("args"), arguments.get("kwargs"), server=resource_scope)
        )
    if cfg.capture_arguments:
        meta["arguments"] = {**arguments, "purpose_id": purpose_id, "data_source_hash": source_hash}
    return meta


def _safe_response(response: Any) -> Any:
    for attr in ("model_dump", "dict"):
        if hasattr(response, attr):
            with contextlib.suppress(Exception):
                return getattr(response, attr)()
    return str(response)
