# Senda-Argus Hooked Docker Runtime

This directory provides Docker runtimes for deploying AI Agents with Senda-Argus Hooks already installed.

The main design goal is to reduce SI work: application teams should not have to add `register()` calls throughout existing Agent source code just to enable audit collection.

## 1. Python runtime - recommended first target

`docker/python/Dockerfile` installs `senda-argus-hooks` and adds a small `.pth` startup hook to Python site-packages. The `.pth` file imports `senda_argus_bootstrap` during interpreter startup, so Senda-Argus is registered before the Agent application starts. A `sitecustomize.py` fallback is also included, but the `.pth` mechanism is primary because it can coexist with an application that already has its own `sitecustomize.py`.

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

Install the customer's Agent dependencies in the child image. At runtime, `sitecustomize` can then import and patch installed OpenAI / Anthropic / LiteLLM / Ollama / MCP / OpenAI Agents packages before application code runs.

## 2. Node runtime - base/preload stage

`docker/node/Dockerfile` installs the JS package and injects a preload with `NODE_OPTIONS=--import=/opt/senda/auto-hook.mjs`.
The preload automatically configures the Senda runtime and exporters.

The current JS SDK instruments concrete client instances (`instrumentOpenAI(client)`, `instrumentAnthropic(client)`, `instrumentMCP(client)`). Therefore this Docker revision does **not** claim complete zero-code interception of arbitrary Node client instances yet. Global client discovery / constructor or loader instrumentation belongs to the next "existing Agent auto-hook" phase.

This distinction is intentional so deployment documentation does not overstate Node coverage.

## 3. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SENDA_ARGUS_ENABLED` | `true` | Enable bootstrap |
| `SENDA_ARGUS_PROJECT` | `default` | Project name |
| `SENDA_ARGUS_ENVIRONMENT` | `prod` | Environment |
| `SENDA_ARGUS_AGENT_ID` | empty | Fixed Agent ID |
| `SENDA_ARGUS_TENANT_ID` | empty | Tenant ID |
| `SENDA_ARGUS_EXPORTER(S)` | `jsonl` | Python: `jsonl`, `stdout`, `argus`, `null`, comma-separated allowed. Node currently supports `jsonl`, `stdout`, `null` |
| `SENDA_ARGUS_JSONL_PATH` | `/var/log/senda-argus/events.jsonl` | JSONL output |
| `SENDA_ARGUS_ENDPOINT` | `http://senda-argus:8000` | Python Argus exporter endpoint |
| `SENDA_ARGUS_API_KEY` | empty | Python Argus exporter API key |
| `SENDA_ARGUS_CAPTURE_PROMPT` | `false` | Store prompt body |
| `SENDA_ARGUS_CAPTURE_RESPONSE` | `false` | Store response body |
| `SENDA_ARGUS_CAPTURE_ARGUMENTS` | `false` | Store tool arguments |
| `SENDA_ARGUS_CAPTURE_RESULT` | `false` | Store tool results |
| `SENDA_ARGUS_CAPTURE_HASH` | `true` | Store hashes |
| `SENDA_ARGUS_REDACT` | `true` | Enable redaction |
| `SENDA_ARGUS_BOOTSTRAP_DEBUG` | `false` | Bootstrap diagnostic messages |

Prompt, response, tool arguments and tool results remain disabled by default. Hashing and redaction remain enabled by default.

## 4. Sending events to Senda-Argus

Python can send directly to the existing Argus exporter:

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

This creates a fake OpenAI package, starts a fresh Python interpreter, verifies that `sitecustomize` automatically patches it, invokes `Completions.create()`, and confirms that an `llm.request` event was written.

## 8. Recommended next phase

Use the Python Docker bootstrap as the reference implementation for the standalone "existing Agent auto-hook" installer:

1. Package the `.pth` startup hook + `senda_argus_bootstrap.py` outside Docker (with `sitecustomize.py` only as a fallback).
2. Add installer/uninstaller and configuration file support.
3. Add Kubernetes injection (initContainer / mutating webhook or admission-based environment injection).
4. For Node, implement global SDK/client interception rather than instance-only instrumentation.
