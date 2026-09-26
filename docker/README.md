# Senda-Argus Hooked Docker Runtime

This directory provides Docker runtimes for deploying AI Agents with Senda-Argus Hooks already installed.

The main design goal is to reduce SI work: application teams should not have to add `register()` calls throughout existing Agent source code just to enable audit collection.

## 1. Python runtime - recommended first target

`docker/python/Dockerfile` installs `senda-argus-hooks` and writes the SDK's `.pth` startup hook to Python site-packages with `senda_argus_hooks.autoinstall`, the same hook the zero-code installer writes. The `.pth` file calls `senda_argus_hooks.autohook.bootstrap()` during interpreter startup, so Senda-Argus is registered before the Agent application starts. A `.pth` hook coexists with an application that already has its own `sitecustomize.py`.

Build:

```bash
docker build -f docker/python/Dockerfile -t senda-argus-python:0.1 .
```

Use it as a base image:

```dockerfile
FROM senda-argus-python:0.1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY . /app
CMD ["python", "/app/agent.py"]
```

No Senda-specific import is required in the Agent source for the SDKs supported by Python `auto_instrument`.

### Important build order

Install the customer's Agent dependencies in the child image. At runtime, the `.pth` hook can then import and patch installed OpenAI / Anthropic / LiteLLM / Ollama / MCP / OpenAI Agents packages before application code runs.

## 2. Node runtime - base/preload stage

`docker/node/Dockerfile` installs the JS package and loads the SDK's zero-code preload with `NODE_OPTIONS=--import=/opt/senda/argus-hooks/dist/zerocode/preload.js`.
The Runtime therefore uses the same preload as the Node zero-code installer. It configures the Senda runtime and exporters from `SENDA_ARGUS_EXPORTER` / `SENDA_ARGUS_EXPORTERS`, including `argus` with `SENDA_ARGUS_ENDPOINT`, `SENDA_ARGUS_API_KEY` and `SENDA_ARGUS_RUN_ID`, and flushes pending events before the process exits.

The preload instruments clients created from the `openai`, `@anthropic-ai/sdk`, `ollama`, `@modelcontextprotocol/sdk/client/index.js` and `@openai/agents` modules when the Agent loads them, for both CommonJS and ES modules. Clients from other modules still need the explicit `instrument*` calls.

When `SENDA_AGENT_INSTALL_DEPS=true` and the mounted workspace has a `package.json`, the entrypoint installs the Agent's dependencies inside the container, with `npm ci` when a lockfile exists and `npm install` otherwise. They are placed under `/opt/senda/node-deps` and exposed as `/node_modules`, which Node searches after the Agent's own `node_modules`, so the host Agent directory is not modified. A SHA-256 marker avoids reinstalling unchanged dependencies on container restart.

## 3. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SENDA_ARGUS_ENABLED` | `true` | Enable bootstrap |
| `SENDA_ARGUS_PROJECT` | `default` | Project name |
| `SENDA_ARGUS_ENVIRONMENT` | `prod` | Environment |
| `SENDA_ARGUS_AGENT_ID` | empty | Fixed Agent ID |
| `SENDA_ARGUS_TENANT_ID` | empty | Tenant ID |
| `SENDA_ARGUS_EXPORTER(S)` | `jsonl` | `jsonl`, `stdout`, `argus`, `null`, comma-separated allowed |
| `SENDA_ARGUS_JSONL_PATH` | `/var/log/senda-argus/events.jsonl` | JSONL output |
| `SENDA_ARGUS_ENDPOINT` | `http://senda-argus:8000` | Argus exporter endpoint |
| `SENDA_ARGUS_API_KEY` | empty | Argus exporter API key |
| `SENDA_AGENT_INSTALL_DEPS` | `false` | Install the mounted workspace's `requirements.txt` or `package.json` before starting the Agent |
| `SENDA_ARGUS_CAPTURE_PROMPT` | `false` | Store prompt body |
| `SENDA_ARGUS_CAPTURE_RESPONSE` | `false` | Store response body |
| `SENDA_ARGUS_CAPTURE_ARGUMENTS` | `false` | Store tool arguments |
| `SENDA_ARGUS_CAPTURE_RESULT` | `false` | Store tool results |
| `SENDA_ARGUS_CAPTURE_HASH` | `true` | Store hashes |
| `SENDA_ARGUS_REDACT` | `true` | Enable redaction |
| `SENDA_ARGUS_BOOTSTRAP_DEBUG` | `false` | Bootstrap diagnostic messages |

Prompt, response, tool arguments and tool results remain disabled by default. Hashing and redaction remain enabled by default.

## 4. Sending events to Senda-Argus

Both runtimes can send directly to the Argus exporter:

```bash
docker run --rm \
  -e SENDA_ARGUS_EXPORTERS=argus,jsonl \
  -e SENDA_ARGUS_ENDPOINT=http://argus.example:8000 \
  -e SENDA_ARGUS_API_KEY=xxxxx \
  senda-argus-python:0.1 python /app/agent.py
```

The exporter posts to:

```text
/v1/agent-runs/ingest
```

If Argus is unavailable, the existing exporter is fail-open and Agent execution continues.

## 5. Docker Compose example

From this directory:

```bash
docker compose -f compose.example.yml up --build
```

The sample Python service writes audit events without calling `register()` in its application startup path.

## 6. Fail-open policy

The bootstrap intentionally catches initialization errors. Observability failure must not stop a production Agent from starting. Set `SENDA_ARGUS_BOOTSTRAP_DEBUG=true` while validating a deployment.

## 7. Local smoke test without Docker

```bash
./docker/tests/smoke_python_autohook.sh
```

This creates a fake OpenAI package, starts a fresh Python interpreter, writes the `.pth` hook with the SDK installer, verifies that the hook automatically patches it, invokes `Completions.create()`, and confirms that an `llm.request` event was written.

## 8. Recommended next phase

Both runtimes now use the SDK's zero-code startup code, so the Docker images and the standalone installers load the same hook. The remaining step is Kubernetes injection, through an initContainer, a mutating webhook or admission-based environment injection.
