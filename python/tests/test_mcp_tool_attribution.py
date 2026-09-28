"""LLM に差し出した候補へ、MCP の一覧から引いたサーバを付けて送ることを固定する。

変更が開く経路:

1. 台帳の規則。名前がそのまま一覧に在るか、サーバの名前を前に付けた形か。1 つに決まらなければ付けない
2. 保持の上限。サーバの数とツールの数を抑え、長い名前は畳む
3. MCP の計装。初期化の応答が名乗ったサーバ名をセッションへ控え、一覧をそのサーバの名前で控える
4. LLM の計装。agent.decision の alternatives の各候補にサーバを付ける

規則は JS の実装と同じで、両方の試験が同じ例を読む。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.core.identity import (
    SERVER_INFO_NAME_ATTR,
    resolve_mcp_server_name,
)
from senda_argus_hooks.core.mcp_tools import (
    McpToolDirectory,
    fold_name,
    get_mcp_tool_directory,
    offered_alternatives,
    tool_names_of,
)

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "js"
    / "tests"
    / "fixtures"
    / "mcp_tool_attribution.json"
)
REF = "drill-ref-a1"
DECOY = "drill-decoy-b2"
REF_TOOL = f"{REF}_lookup_order"
DECOY_TOOL = f"{DECOY}_lookup_order"


@pytest.fixture(autouse=True)
def _clean_directory():
    get_mcp_tool_directory().clear()
    yield
    get_mcp_tool_directory().clear()


def _fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _fixture()["cases"], ids=lambda c: c["name"])
def test_the_attribution_rule_matches_the_shared_examples(case):
    directory = McpToolDirectory()
    for server, tools in case["servers"]:
        directory.record(server, tools)
    for name, expected in case["lookups"].items():
        assert directory.server_of(name) == expected, name


@pytest.mark.parametrize(
    "vector", _fixture()["fold"], ids=lambda v: str(len(v["input"]))
)
def test_the_fold_matches_the_shared_examples(vector):
    assert fold_name(vector["input"]) == vector["expected"]


def test_an_unattributed_candidate_carries_no_server_key():
    get_mcp_tool_directory().record("alpha", ["search"])
    assert offered_alternatives(["search", "unlisted"]) == [
        {"name": "search", "mcp_server": "alpha"},
        {"name": "unlisted"},
    ]


def test_the_oldest_server_is_dropped_beyond_the_server_limit():
    directory = McpToolDirectory(max_servers=2)
    directory.record("s1", ["t1"])
    directory.record("s2", ["t2"])
    directory.record("s3", ["t3"])
    assert directory.server_of("t1") == ""
    assert directory.server_of("t2") == "s2"
    assert directory.server_of("t3") == "s3"


def test_recording_a_server_again_refreshes_its_place():
    directory = McpToolDirectory(max_servers=2)
    directory.record("s1", ["t1"])
    directory.record("s2", ["t2"])
    directory.record("s1", ["t1b"])
    directory.record("s3", ["t3"])
    assert directory.server_of("t1") == "s1"
    assert directory.server_of("t2") == ""


def test_tools_beyond_the_per_server_limit_are_not_recorded():
    directory = McpToolDirectory(max_tools_per_server=2)
    directory.record("s", ["a", "b", "c"])
    assert directory.server_of("b") == "s"
    assert directory.server_of("c") == ""


@pytest.mark.parametrize(
    "response",
    [
        types.SimpleNamespace(
            tools=[types.SimpleNamespace(name="a"), types.SimpleNamespace(name="b")]
        ),
        {"tools": [{"name": "a"}, {"name": "b"}]},
        [{"name": "a"}, {"name": "b"}],
    ],
    ids=["sdk", "dict", "list"],
)
def test_tool_names_are_read_from_each_listing_shape(response):
    assert tool_names_of(response) == ["a", "b"]


def _install_fake_mcp(monkeypatch):
    class ClientSession:
        def __init__(self, announced: str, tools: list[str], **attrs):
            self._announced = announced
            self._tools = tools
            for key, value in attrs.items():
                setattr(self, key, value)

        async def initialize(self):
            return types.SimpleNamespace(
                serverInfo=types.SimpleNamespace(name=self._announced, version="1")
            )

        async def list_tools(self):
            return types.SimpleNamespace(
                tools=[types.SimpleNamespace(name=n) for n in self._tools]
            )

        async def call_tool(self, name, arguments=None, **kwargs):
            return {"ok": True}

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = ClientSession
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    return ClientSession


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


def _install_fake_openai(monkeypatch, selected: str):
    class Completions:
        def create(self, *args, **kwargs):
            return _Response(
                {
                    "id": "chatcmpl_fake",
                    "model": kwargs.get("model"),
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"function": {"name": selected, "arguments": "{}"}}
                                ]
                            }
                        }
                    ],
                }
            )

    class Responses:
        def create(self, *args, **kwargs):
            return _Response({"id": "resp_fake"})

    class Embeddings:
        def create(self, *args, **kwargs):
            return _Response({"id": "emb_fake"})

    fake_openai = types.ModuleType("openai")
    fake_openai.resources = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(Completions=Completions)
        ),
        responses=types.SimpleNamespace(Responses=Responses),
        embeddings=types.SimpleNamespace(Embeddings=Embeddings),
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    return Completions


def _register(path: Path) -> None:
    register(
        project="test-offered",
        exporters=[{"type": "jsonl", "path": str(path)}],
        auto_instrument=True,
        instrument_anthropic=False,
        instrument_litellm=False,
        instrument_argus_sdk=False,
    )


def _events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run_agent(
    session_cls, completions_cls, sessions: list, *, initialize: bool
) -> None:
    async def body():
        for session in sessions:
            if initialize:
                await session.initialize()
            await session.list_tools()
        completions_cls().create(
            model="gpt-fake",
            messages=[{"role": "user", "content": "look up order 1234"}],
            tools=[
                {"type": "function", "function": {"name": REF_TOOL}},
                {"type": "function", "function": {"name": DECOY_TOOL}},
            ],
        )
        await sessions[1].call_tool(DECOY_TOOL, {"order_id": "1234"})

    asyncio.run(body())


def test_the_decision_carries_the_server_of_each_candidate(tmp_path, monkeypatch):
    session_cls = _install_fake_mcp(monkeypatch)
    completions_cls = _install_fake_openai(monkeypatch, DECOY_TOOL)
    path = tmp_path / "events.jsonl"
    _register(path)
    try:
        sessions = [session_cls(REF, [REF_TOOL]), session_cls(DECOY, [DECOY_TOOL])]
        _run_agent(session_cls, completions_cls, sessions, initialize=True)
    finally:
        shutdown()
    events = _events(path)
    decisions = [e for e in events if e["event_type"] == "agent.decision"]
    assert len(decisions) == 1
    assert decisions[0]["data"]["selected_tool"] == DECOY_TOOL
    assert decisions[0]["data"]["alternatives"] == [
        {"name": REF_TOOL, "mcp_server": REF},
        {"name": DECOY_TOOL, "mcp_server": DECOY},
    ]
    # 呼び出しの記録も同じ名前で出る。Argus はこの名前で観測したサーバを一覧へ載せ、承認もこの名前で判定する。
    completed = [e for e in events if e["event_type"] == "mcp.tool_call.completed"]
    assert completed[0]["data"]["mcp"]["server"] == DECOY
    # 初期化は事象を出さない。
    assert not [e for e in events if "initialize" in e["event_type"]]


def test_without_the_initialize_response_the_candidates_carry_no_server(
    tmp_path, monkeypatch
):
    """初期化を通らないセッションは名前を持たず、一覧を控えない。候補には何も付けない。"""
    session_cls = _install_fake_mcp(monkeypatch)
    completions_cls = _install_fake_openai(monkeypatch, DECOY_TOOL)
    path = tmp_path / "events.jsonl"
    _register(path)
    try:
        sessions = [session_cls(REF, [REF_TOOL]), session_cls(DECOY, [DECOY_TOOL])]
        _run_agent(session_cls, completions_cls, sessions, initialize=False)
    finally:
        shutdown()
    decisions = [e for e in _events(path) if e["event_type"] == "agent.decision"]
    assert decisions[0]["data"]["alternatives"] == [
        {"name": REF_TOOL},
        {"name": DECOY_TOOL},
    ]


def test_an_explicit_session_name_wins_over_the_announced_name(tmp_path, monkeypatch):
    session_cls = _install_fake_mcp(monkeypatch)
    path = tmp_path / "events.jsonl"
    _register(path)
    try:
        session = session_cls("announced", ["t"], server="explicit")
        asyncio.run(session.initialize())
        assert getattr(session, SERVER_INFO_NAME_ATTR) == "announced"
        assert resolve_mcp_server_name(session) == "explicit"
    finally:
        shutdown()


def test_the_announced_name_is_read_after_the_explicit_attributes():
    session = types.SimpleNamespace(**{SERVER_INFO_NAME_ATTR: "announced"})
    assert resolve_mcp_server_name(session) == "announced"


def test_two_sessions_announcing_the_same_name_get_no_attribution(
    tmp_path, monkeypatch
):
    """偽のサーバが本物と同じ名前を名乗る。どちらの候補にもその名前を付けず、呼び出しも名前を持たない。"""
    session_cls = _install_fake_mcp(monkeypatch)
    completions_cls = _install_fake_openai(monkeypatch, DECOY_TOOL)
    path = tmp_path / "events.jsonl"
    _register(path)
    try:
        # おとりが見本の名前を名乗る。ツールの名前は違っても、名乗りだけで帰属が決まる状態を作らない。
        sessions = [session_cls(REF, [REF_TOOL]), session_cls(REF, [DECOY_TOOL])]
        _run_agent(session_cls, completions_cls, sessions, initialize=True)
    finally:
        shutdown()
    events = _events(path)
    decisions = [e for e in events if e["event_type"] == "agent.decision"]
    assert decisions[0]["data"]["alternatives"] == [
        {"name": REF_TOOL},
        {"name": DECOY_TOOL},
    ]
    completed = [e for e in events if e["event_type"] == "mcp.tool_call.completed"]
    assert completed[0]["data"]["mcp"]["server"] == "unknown"


def test_one_session_announcing_twice_is_not_a_conflict():
    class Session:
        pass

    directory = McpToolDirectory()
    session = Session()
    assert directory.claim("s", session) is True
    assert directory.claim("s", session) is True
    assert directory.is_conflicted("s") is False


class _Session:
    pass


def test_an_impostor_using_an_explicit_name_conflicts():
    """正規のセッションは明示の名前で一覧を控え、偽のセッションが同じ名前を名乗る。"""
    directory = McpToolDirectory()
    real, impostor = _Session(), _Session()
    directory.record("github", ["search"], session=real)
    assert directory.claim("github", impostor) is False
    directory.record("github", ["evil_search"], session=impostor)
    assert directory.server_of("search") == ""
    assert directory.server_of("evil_search") == ""


def test_an_impostor_that_closed_first_leaves_no_tools_behind():
    import gc

    directory = McpToolDirectory()
    impostor = _Session()
    directory.record("github", ["evil_search"], session=impostor)
    del impostor
    gc.collect()
    real = _Session()
    directory.record("github", ["search"], session=real)
    assert directory.server_of("evil_search") == ""
    assert directory.server_of("search") == "github"
