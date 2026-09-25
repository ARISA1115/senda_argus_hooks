# Senda Agent Studio v0.1.0

Initial MVP implementing the five requested functions:

- Managed Agent list
- Docker start/stop/restart
- Agent create/deploy
- Real-time Hook event view over SSE
- Agent trace view backed by Hook events

Hook event ingest remains compatible with the existing Senda-Argus `/v1/agent-runs/ingest` payload.
