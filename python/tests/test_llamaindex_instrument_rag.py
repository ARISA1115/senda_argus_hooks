import asyncio
import json
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.integrations import instrument_rag


def _events(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class FakeRetriever:
    name = "security_kb_retriever"

    def retrieve(self, query):
        return [{"node_id": "chunk-1", "doc_id": "doc-1", "score": 0.9}]

    async def aretrieve(self, query):
        await asyncio.sleep(0)
        return [{"node_id": "chunk-1", "doc_id": "doc-1", "score": 0.9}]


class FakeEmbedModel:
    model_name = "local-test-embedding"

    def get_text_embedding(self, text):
        return [0.1, 0.2, 0.3]

    def get_text_embeddings(self, texts):
        return [[0.1, 0.2], [0.3, 0.4]]

    async def aget_text_embedding(self, text):
        await asyncio.sleep(0)
        return [0.1, 0.2, 0.3]

    async def aget_text_embeddings(self, texts):
        await asyncio.sleep(0)
        return [[0.1, 0.2], [0.3, 0.4]]


class FakeQueryEngine:
    name = "security_query_engine"

    def query(self, query):
        return {"answer": "ok", "source_nodes": [{"node": {"text": "context chunk 1"}}]}

    async def aquery(self, query):
        await asyncio.sleep(0)
        return {"answer": "ok", "source_nodes": [{"node": {"text": "context chunk 1"}}]}


def test_instrument_rag_patches_components_with_one_call(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    retriever = FakeRetriever()
    embed_model = FakeEmbedModel()
    query_engine = FakeQueryEngine()

    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], capture_arguments=True, capture_result=False)

    handle = instrument_rag(
        retriever=retriever,
        embed_model=embed_model,
        query_engine=query_engine,
        framework="llamaindex",
        retriever_type="vector",
        index_name="security_knowledge_base",
        vector_store="faiss",
        top_k=1,
        provider="local",
    )

    assert handle.installed()
    assert retriever.retrieve("CVE-2024-3094")
    assert embed_model.get_text_embedding("CVE-2024-3094") == [0.1, 0.2, 0.3]
    assert query_engine.query("CVE-2024-3094")["answer"] == "ok"

    shutdown()

    assert [event["event_type"] for event in _events(path)] == [
        "retrieval.requested",
        "retrieval.completed",
        "embedding.requested",
        "embedding.completed",
        "rag.query.started",
        "rag.query.completed",
    ]

    query_completed = [e for e in _events(path) if e["event_type"] == "rag.query.completed"][0]
    assert query_completed["data"]["rag"]["context_count"] == 1
    assert query_completed["data"]["rag"]["context_hash"]


def test_instrument_rag_awaits_async_methods_before_reporting_completion(tmp_path: Path):
    """非同期版が実行前のコルーチンではなく実際の結果を completed イベントに反映することを確認する。

    以前は非同期メソッドを await せずに result_hash/context_hash 等を計算しており、
    未完了のコルーチンオブジェクトをハッシュ化した誤った completed イベントを送出していた。
    """
    path = tmp_path / "events.jsonl"
    retriever = FakeRetriever()
    embed_model = FakeEmbedModel()
    query_engine = FakeQueryEngine()

    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], capture_arguments=True, capture_result=False)

    instrument_rag(
        retriever=retriever,
        embed_model=embed_model,
        query_engine=query_engine,
        framework="llamaindex",
    )

    async def _run():
        await retriever.aretrieve("CVE-2024-3094")
        await embed_model.aget_text_embedding("CVE-2024-3094")
        await query_engine.aquery("CVE-2024-3094")

    asyncio.run(_run())
    shutdown()

    events = _events(path)
    retrieval_completed = [e for e in events if e["event_type"] == "retrieval.completed"][0]
    assert retrieval_completed["data"]["retrieval"]["result_count"] == 1

    embedding_completed = [e for e in events if e["event_type"] == "embedding.completed"][0]
    assert embedding_completed["data"]["embedding"]["vector_dimension"] == 3

    query_completed = [e for e in events if e["event_type"] == "rag.query.completed"][0]
    assert query_completed["data"]["rag"]["context_count"] == 1
    assert query_completed["data"]["rag"]["context_hash"]


def test_register_can_instrument_rag_components(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    retriever = FakeRetriever()

    result = register(
        project="test",
        exporters=[{"type": "jsonl", "path": str(path)}],
        rag={
            "retriever": retriever,
            "framework": "llamaindex",
            "retriever_type": "vector",
            "index_name": "security_knowledge_base",
        },
    )

    assert result["instrumentors"]["rag"] is True
    retriever.retrieve("CVE-2024-3094")
    shutdown()

    assert [event["event_type"] for event in _events(path)] == ["retrieval.requested", "retrieval.completed"]
