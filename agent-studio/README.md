# Senda Agent Studio MVP

Minimal WebUI for Senda-Argus managed Agent containers.

## MVP functions

1. Agent list
2. Docker start / stop / restart
3. Agent creation/deploy
4. Real-time Hook event display (SSE)
5. Agent detail trace

## Architecture

- FastAPI management API + static WebUI
- Docker Engine via `/var/run/docker.sock`
- Hook ingest endpoint compatible with existing SDK: `POST /v1/agent-runs/ingest`
- SQLite event store at `/data/studio.db`
- SSE at `/api/events/stream`

## Start

```bash
docker compose -f agent-studio/compose.yml up -d --build
```

Open: `http://HOST:8080`

The Studio container and managed Agent containers should share `senda-agent-net`.
When creating an Agent in the UI, use this default Hook endpoint:

```text
http://senda-agent-studio:8080
```

The managed container receives:

```text
SENDA_ARGUS_ENABLED=true
SENDA_ARGUS_EXPORTER=argus
SENDA_ARGUS_EXPORTERS=argus
SENDA_ARGUS_ENDPOINT=http://senda-agent-studio:8080
SENDA_ARGUS_AGENT_ID=<agent id>
SENDA_ARGUS_PROJECT=<project>
SENDA_ARGUS_ENVIRONMENT=<environment>
```

## Managed container labels

Only containers with `com.senda.agent.managed=true` appear in the Agent list.

## Security note

This MVP mounts the Docker socket for simplicity. Access to the Docker socket is effectively host-level control. In production, place Studio behind authentication and replace direct socket access with a restricted Docker API proxy or dedicated runtime controller.

Environment values submitted at Agent creation are passed to Docker and can include secrets. For production, replace this with Docker/Kubernetes secrets or an external secret manager.

## Existing Senda Hook runtime

Use the Hooked Python/Node images produced in the previous phases as the `Image` field. The Agent application itself does not need a Senda-specific import when the image/runtime auto-hook is active.

## Optional upstream Senda-Argus forwarding

Studio can retain events for the UI and also forward the same payload to a central Senda-Argus instance:

```text
SENDA_STUDIO_ARGUS_UPSTREAM=https://argus.example.local
SENDA_STUDIO_ARGUS_API_KEY=...
```

Forwarding is fail-open and does not block Agent execution if the upstream is unavailable.
