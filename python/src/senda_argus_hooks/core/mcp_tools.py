"""MCP のツール一覧を観測して、LLM に差し出したツールの名前からサーバを引く台帳。

Argus の選択誘導の検知は、LLM に差し出した候補のそれぞれがどの MCP サーバのものかを読む。候補の名前は
LLM の呼び出しの引数から取れるが、サーバは取れない。サーバが分かるのは MCP の ``list_tools`` の結果で、
セッションがどのサーバのものかも分かっている。一覧を受けたときにサーバごとにツールの名前を控え、
LLM の呼び出しで候補を送るときにここから引く。

**引けないときは何も載せない。** 同じ名前が 2 つのサーバに在る、どのサーバにも無い、といった場合に
どれかを既定として当てると、信頼したサーバの帰属を別のツールへ付けることになり、判定が逆になる。

**名前の突き合わせは 2 通りだけにする。** 名前がそのまま一覧に在るか、サーバの名前を前に付けて
``<server>__<tool>`` か ``<server>_<tool>`` の形で差し出されたか。後者はツールの名前が重ならないよう
フレームワークが付ける形で、Argus の規則も同じ 2 つの区切りを読む。

**保持には上限がある。** サーバの数とサーバごとのツールの数を抑え、超えたら最も古く控えたサーバから
外す。長い名前は捨てずに畳む。捨てると、長い名前を選ぶだけでそのツールの帰属が引けなくなる。

JS の実装 (js/src/core/mcp_tools.ts) も同じ規則を持つ。規則を変えるときは両方を変え、両方の試験が読む
共通の例 (js/tests/fixtures/mcp_tool_attribution.json) を足す。
"""

from __future__ import annotations

import hashlib
import threading
import weakref
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from .identity import UNNAMED_MCP_SERVER

MAX_SERVERS = 64
MAX_TOOLS_PER_SERVER = 512
# これより長い名前は SHA-256 に畳んで控え、引くときも同じく畳んでから比べる。
MAX_NAME_LEN = 256
_SEPARATORS = ("__", "_")


def fold_name(name: str) -> str:
    """控えと照合に使う形。長い名前は畳む。"""
    if len(name) <= MAX_NAME_LEN:
        return name
    return "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()


class McpToolDirectory:
    """サーバ名ごとに、``list_tools`` で観測したツールの名前を控える。"""

    def __init__(
        self,
        *,
        max_servers: int = MAX_SERVERS,
        max_tools_per_server: int = MAX_TOOLS_PER_SERVER,
    ) -> None:
        self._lock = threading.Lock()
        self._servers: OrderedDict[str, set[str]] = OrderedDict()
        self._max_servers = max_servers
        self._max_tools = max_tools_per_server
        # 名前ごとに、その名前でツールを控えたセッション。名乗りはサーバが決め、明示の名前も複数の
        # セッションに付けうる。同じ名前を 2 つの生きたセッションが使ったら衝突として扱い、以後どの
        # ツールにもその名前を付けない。付けると、承認済みのサーバの名前を名乗った偽のツールが承認済みの
        # サーバのものとして送られ、誘導の検知が止まる。前のセッションが消えた後に別のセッションが
        # 同じ名前を使ったら、控えを捨ててから引き継ぐ。前のセッションのツールを残さない。
        self._owners: dict[str, weakref.ref] = {}
        self._conflicted: set[str] = set()

    def record(
        self, server: Any, tool_names: Iterable[Any], session: Any = None
    ) -> None:
        """一覧の 1 回分を控える。

        一覧は頁に分かれて届くことがあるため、前に控えた名前へ足す。置き換えると、後の頁を受けたときに
        前の頁のツールの帰属が引けなくなる。名前を持たないセッションは控えない。どのサーバか分からない
        一覧から帰属を作ると、名前を持たない全てのセッションのツールが同じサーバのものに見える。

        計装は一覧を受けたセッションを渡す。名前の持ち主は 1 つの生きたセッションに限る。
        """
        server_name = str(server or "").strip()
        if not server_name or server_name == UNNAMED_MCP_SERVER:
            return
        names = [fold_name(str(n)) for n in tool_names if isinstance(n, str) and n]
        if not names:
            return
        with self._lock:
            if session is not None and not self._claim_locked(server_name, session):
                return
            if server_name in self._conflicted:
                return
            known = self._servers.pop(server_name, set())
            for name in names:
                if name in known:
                    continue
                if len(known) >= self._max_tools:
                    break
                known.add(name)
            self._servers[server_name] = known
            while len(self._servers) > self._max_servers:
                gone, _ = self._servers.popitem(last=False)
                self._owners.pop(gone, None)

    def claim(self, server: str, session: Any) -> bool:
        """名前をセッションに結び付ける。衝突していなければ True。"""
        with self._lock:
            return self._claim_locked(server, session)

    def _claim_locked(self, server: str, session: Any) -> bool:
        if server in self._conflicted:
            return False
        try:
            current = self._owners.get(server)
            owner = current() if current is not None else None
            if owner is session:
                return True
            if owner is not None:
                # 2 つの生きたセッションが同じ名前を使った。どちらのツールにもその名前を付けない。
                self._conflicted.add(server)
                self._servers.pop(server, None)
                self._owners.pop(server, None)
                return False
            # 持ち主が居ないか消えた。前の持ち主のツールを捨ててから引き継ぐ。
            self._servers.pop(server, None)
            self._owners[server] = weakref.ref(session)
        except TypeError:
            # 弱参照を持てないセッションは持ち主を確かめられない。その名前を付けない。
            return False
        while len(self._owners) > self._max_servers:
            self._owners.pop(next(iter(self._owners)))
        return True

    def is_conflicted(self, server: Any) -> bool:
        with self._lock:
            return server in self._conflicted

    def server_of(self, name: Any) -> str:
        """差し出した名前のサーバを返す。1 つに決まらなければ空文字を返す。"""
        if not isinstance(name, str) or not name:
            return ""
        with self._lock:
            servers = list(self._servers.items())
        folded = fold_name(name)
        exact = {server for server, tools in servers if folded in tools}
        if exact:
            return next(iter(exact)) if len(exact) == 1 else ""
        prefixed: set[str] = set()
        for server, tools in servers:
            for sep in _SEPARATORS:
                head = server + sep
                if name.startswith(head) and fold_name(name[len(head) :]) in tools:
                    prefixed.add(server)
        return next(iter(prefixed)) if len(prefixed) == 1 else ""

    def alternatives(self, offered: Iterable[str]) -> list[dict[str, str]]:
        """候補の一覧を、引けたものにだけサーバを付けて返す。"""
        out: list[dict[str, str]] = []
        for name in offered:
            entry = {"name": name}
            server = self.server_of(name)
            if server:
                entry["mcp_server"] = server
            out.append(entry)
        return out

    def clear(self) -> None:
        with self._lock:
            self._servers.clear()
            self._owners.clear()
            self._conflicted.clear()


_directory = McpToolDirectory()


def get_mcp_tool_directory() -> McpToolDirectory:
    return _directory


def offered_alternatives(offered: Iterable[str]) -> list[dict[str, str]]:
    """計装が候補を送るときの唯一の組み立て。全ての計装がここを通る。"""
    return _directory.alternatives(offered)


def tool_names_of(response: Any) -> list[str]:
    """``list_tools`` の応答からツールの名前を取り出す。

    SDK の応答は ``tools`` を持つ。辞書の応答と、ツールの並びそのものも受ける。
    """
    tools = getattr(response, "tools", None)
    if tools is None and isinstance(response, dict):
        tools = response.get("tools")
    if tools is None and isinstance(response, (list, tuple)):
        tools = response
    names: list[str] = []
    if not isinstance(tools, (list, tuple)):
        return names
    for tool in tools:
        name = (
            tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        )
        if isinstance(name, str) and name:
            names.append(name)
    return names


def read_only_tool_names_of(response: Any) -> list[str]:
    """list_tools の応答から、提供元が読み取りだけと宣言したツールの名前を取り出す。"""
    tools = getattr(response, "tools", None)
    if tools is None and isinstance(response, dict):
        tools = response.get("tools")
    if tools is None and isinstance(response, (list, tuple)):
        tools = response
    names: list[str] = []
    if not isinstance(tools, (list, tuple)):
        return names
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name")
            annotations = tool.get("annotations")
            hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) else None
        else:
            name = getattr(tool, "name", None)
            annotations = getattr(tool, "annotations", None)
            hint = getattr(annotations, "readOnlyHint", None)
        if isinstance(name, str) and name and hint is True:
            names.append(name)
    return names
