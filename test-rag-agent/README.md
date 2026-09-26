# test-rag-agent

A deterministic, dependency-free RAG test runtime for Senda Arugus Agent Studio.

The Agent uses local embedded documents and calls normal component methods after `instrument_rag(...)` patches the supplied instances.

Expected Agent Trace events:

- `embedding.requested` / `embedding.completed`
- `retrieval.requested` / `retrieval.completed`
- `rag.query.started` / `rag.query.completed`

Expected Docker Logs also include `[senda-argus-http] POST .../v1/agent-runs/ingest` and a completion status.

Register this directory in Agent Studio as a Python runtime with `agent.py` as the entrypoint.
