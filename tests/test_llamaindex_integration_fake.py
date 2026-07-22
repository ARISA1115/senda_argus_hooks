import asyncio
import json
from pathlib import Path

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.integrations.llamaindex import (
    SendaArgusLlamaIndexCallbackHandler,
    aretrieve_with_argus,
    embed_text_with_argus,
    embed_texts_with_argus,
    query_with_argus,
    retrieve_with_argus,
)


def _events(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class FakeNode:
    def __init__(self, node_id: str, ref_doc_id: str):
        self.node_id = node_id
        self.ref_doc_id = ref_doc_id
        self.metadata = {"source": "kb"}


class FakeNodeWithScore:
    def __init__(self, node_id: str, ref_doc_id: str, score: float):
        self.node = FakeNode(node_id, ref_doc_id)
        self.score = score


class FakeRetriever:
    name = "security_kb_retriever"

    def retrieve(self, query):
        return [FakeNodeWithScore("chunk-1", "doc-1", 0.91), FakeNodeWithScore("chunk-2", "doc-1", 0.82)]

    async def aretrieve(self, query):
        return [FakeNodeWithScore("chunk-3", "doc-2", 0.77)]


class FailedRetriever:
    name = "failed_retriever"

    def retrieve(self, query):
        raise RuntimeError("retrieval failed")


class FakeEmbedModel:
    model_name = "local-test-embedding"

    def get_text_embedding(self, text):
        return [0.1, 0.2, 0.3]

    def get_text_embeddings(self, texts):
        return [[0.1, 0.2], [0.3, 0.4]]


class FakeQueryEngine:
    name = "security_query_engine"

    def query(self, query):
        return {
            "answer": "CVE-2024-3094 is related to xz-utils.",
            "source_nodes": [FakeNodeWithScore("chunk-1", "doc-1", 0.91)],
        }


class FailedQueryEngine:
    def query(self, query):
        raise ValueError("query failed")


def test_llamaindex_retrieve_with_argus_emits_retrieval_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], capture_arguments=True, capture_result=False)

    result = retrieve_with_argus(
        FakeRetriever(),
        "CVE-2024-3094",
        retriever_type="vector",
        index_name="security_knowledge_base",
        vector_store="faiss",
        top_k=2,
    )
    shutdown()

    assert len(result) == 2
    events = _events(path)
    assert [event["event_type"] for event in events] == ["retrieval.requested", "retrieval.completed"]
    completed = events[-1]
    assert completed["purpose_id"].startswith("purpose_")
    retrieval = completed["data"]["retrieval"]
    assert retrieval["framework"] == "llamaindex"
    assert retrieval["retriever_name"] == "security_kb_retriever"
    assert retrieval["result_count"] == 2
    assert retrieval["score_min"] == 0.82
    assert retrieval["score_max"] == 0.91
    assert retrieval["document_ids_hash"].startswith("sha256:")
    assert retrieval["chunk_ids_hash"].startswith("sha256:")


def test_llamaindex_retrieve_with_argus_emits_failed_event(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}])

    try:
        retrieve_with_argus(FailedRetriever(), "hello")
    except RuntimeError:
        pass
    shutdown()

    events = _events(path)
    assert [event["event_type"] for event in events] == ["retrieval.requested", "retrieval.failed"]
    assert events[-1]["error"]["type"] == "RuntimeError"


def test_llamaindex_aretrieve_with_argus_emits_retrieval_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}])

    result = asyncio.run(aretrieve_with_argus(FakeRetriever(), "hello", retriever_type="vector"))
    shutdown()

    assert len(result) == 1
    assert [event["event_type"] for event in _events(path)] == ["retrieval.requested", "retrieval.completed"]


def test_llamaindex_embedding_wrappers_emit_embedding_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], capture_arguments=True)

    one = embed_text_with_argus(FakeEmbedModel(), "hello", provider="local")
    many = embed_texts_with_argus(FakeEmbedModel(), ["hello", "world"], provider="local")
    shutdown()

    assert one == [0.1, 0.2, 0.3]
    assert many == [[0.1, 0.2], [0.3, 0.4]]
    events = _events(path)
    assert [event["event_type"] for event in events] == [
        "embedding.requested",
        "embedding.completed",
        "embedding.requested",
        "embedding.completed",
    ]
    assert events[1]["data"]["embedding"]["vector_dimension"] == 3
    assert events[3]["data"]["embedding"]["vector_dimension"] == 2
    assert events[3]["data"]["embedding"]["vector_count"] == 2
    assert events[1]["data"]["embedding"]["vector_sketch"] == [0.1, 0.2, 0.3]


def test_llamaindex_query_with_argus_emits_rag_query_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}], capture_result=True)

    result = query_with_argus(FakeQueryEngine(), "CVE-2024-3094")
    shutdown()

    assert result["answer"].startswith("CVE")
    events = _events(path)
    assert [event["event_type"] for event in events] == ["rag.query.started", "rag.query.completed"]
    assert events[-1]["data"]["rag"]["query_engine"] == "security_query_engine"
    assert events[-1]["data"]["rag"]["context_count"] == 1
    assert events[-1]["data"]["rag"]["context_hash"]


def test_llamaindex_callback_handler_emits_events(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    register(project="test", exporters=[{"type": "jsonl", "path": str(path)}])

    handler = SendaArgusLlamaIndexCallbackHandler()
    handler.on_retrieval_start("hello", run_id="r1", retriever_name="kb")
    handler.on_retrieval_end([{"node_id": "chunk-1", "doc_id": "doc-1", "score": 0.5}], run_id="r1", retriever_name="kb")
    handler.on_embedding_start("hello", run_id="e1", model="local")
    handler.on_embedding_end([0.1, 0.2], run_id="e1", model="local")
    shutdown()

    assert [event["event_type"] for event in _events(path)] == [
        "retrieval.requested",
        "retrieval.completed",
        "embedding.requested",
        "embedding.completed",
    ]
