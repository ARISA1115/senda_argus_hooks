"""実行主体の識別子を導く公開関数の互換を固定する。"""

from __future__ import annotations

from senda_argus_hooks.core.identity import derive_agent_id


def test_the_sdk_keyword_is_still_accepted_and_ignored() -> None:
    """sdk の名前で値を渡す既存の呼び出しが落ちず、識別子も変わらないこと。

    どの取り込みが出したかを識別子から外したため、sdk は値に混ぜない。引数ごと消すと、更新した
    だけで既存の呼び出し元が型の誤りで落ちる。
    """
    base = derive_agent_id(project="p", environment="e", agent_hint="a", runtime="r")
    assert derive_agent_id(project="p", environment="e", agent_hint="a", runtime="r", sdk="openai") == base
    assert derive_agent_id(project="p", environment="e", agent_hint="a", runtime="r", sdk="anthropic") == base
