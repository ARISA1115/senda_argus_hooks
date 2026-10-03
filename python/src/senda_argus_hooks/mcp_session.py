"""MCP のセッションに、計装が読むサーバの名前と URL を明示する。

Python の MCP の SDK の ``ClientSession`` は接続先の URL を持たない。Argus は提供元を URL で識別し、
自分で取得した一覧と突き合わせるため、URL が無いセッションの一覧は判定に使えない。JS の計装が
``mcpMetadata.serverUrl`` で受けるのと同じ値を、Python ではこの関数で渡す。

明示しなければ何も載せない。推測した URL を載せると、別の提供元の取得と突き合わされる。
"""

from __future__ import annotations

from typing import TypeVar

from .core.identity import SERVER_URL_ATTR

T = TypeVar("T")


def describe_mcp_session(session: T, *, server_url: str | None = None, server_name: str | None = None) -> T:
    """セッションへサーバの URL と名前を控えて、同じセッションを返す。"""
    if server_url:
        setattr(session, SERVER_URL_ATTR, str(server_url))
    if server_name:
        session.server_name = str(server_name)  # type: ignore[attr-defined]
    return session
