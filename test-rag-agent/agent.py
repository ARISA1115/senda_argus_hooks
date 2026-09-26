from __future__ import annotations

import math
from dataclasses import dataclass

from senda_argus_hooks import flush
from senda_argus_hooks.integrations import instrument_rag


DOCS = [
    {
        "doc_id": "kb-001",
        "text": "CVE-2024-3094 is associated with XZ Utils and is useful as a deterministic RAG test record.",
    },
    {
        "doc_id": "kb-002",
        "text": "MCP tool calls are useful for testing external-tool observability in an agent runtime.",
    },
    {
        "doc_id": "kb-003",
        "text": "Senda-Nexus combines external observation data, CVE context, and security intelligence workflows.",
    },
    {
        "doc_id": "kb-004",
        "text": "A RAG pipeline retrieves relevant chunks before a query engine creates an answer from context.",
    },
]


def _tokens(text: str) -> set[str]:
    return {token.strip(".,:/()[]{}\"'").lower() for token in text.split() if token.strip()}


class LocalRetriever:
    name = "senda_local_kb_retriever"

    def retrieve(self, query: str):
        q = _tokens(query)
        scored = []
        for idx, doc in enumerate(DOCS):
            d = _tokens(doc["text"])
            overlap = len(q & d)
            score = overlap / max(1.0, math.sqrt(len(q) * len(d)))
            scored.append(
                {
                    "node_id": f"chunk-{idx + 1}",
                    "doc_id": doc["doc_id"],
                    "text": doc["text"],
                    "score": round(score, 4),
                }
            )
        return sorted(scored, key=lambda item: item["score"], reverse=True)[:2]


class LocalEmbedModel:
    model_name = "senda-test-hash-embedding-v1"

    def get_text_embedding(self, text: str):
        # Small deterministic vector: enough to exercise embedding trace events.
        tokens = sorted(_tokens(text))
        return [
            float(len(tokens)),
            float(sum(len(t) for t in tokens)),
            float(sum(ord(ch) for ch in text) % 997),
            float(len(text)),
        ]


@dataclass
class LocalQueryEngine:
    retriever: LocalRetriever
    name: str = "senda_local_rag_query_engine"

    def query(self, query: str):
        hits = self.retriever.retrieve(query)
        best = hits[0] if hits else None
        answer = best["text"] if best else "No matching local context."
        return {
            "answer": answer,
            "source_nodes": [
                {"node": {"text": item["text"], "doc_id": item["doc_id"]}, "score": item["score"]}
                for item in hits
            ],
        }


def main() -> None:
    retriever = LocalRetriever()
    embed_model = LocalEmbedModel()
    query_engine = LocalQueryEngine(retriever)

    handle = instrument_rag(
        retriever=retriever,
        embed_model=embed_model,
        query_engine=query_engine,
        framework="senda-local-rag",
        retriever_type="keyword",
        index_name="senda-test-kb",
        collection_name="test-documents",
        vector_store="in-memory",
        top_k=2,
        provider="local",
        embedding_model=embed_model.model_name,
        query_engine_name=query_engine.name,
        source="local-test-documents",
        source_type="embedded-test-data",
    )

    print(f"[test-rag-agent] instrumentation={handle.installed()}", flush=True)
    query = "What does CVE-2024-3094 relate to?"

    # Call each normal application method after instrumentation.  No wrapper calls are
    # needed in application code; the hooks emit the events transparently.
    vector = embed_model.get_text_embedding(query)
    hits = retriever.retrieve(query)
    response = query_engine.query(query)

    print(f"[test-rag-agent] embedding_dimension={len(vector)}", flush=True)
    print(f"[test-rag-agent] retrieved={[(h['doc_id'], h['score']) for h in hits]}", flush=True)
    print(f"[test-rag-agent] answer={response['answer']}", flush=True)

    flush()
    print("[test-rag-agent] completed; open Agent Trace and Docker Logs", flush=True)


if __name__ == "__main__":
    main()
