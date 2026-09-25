# Senda-Argus Deployment Guide

This document consolidates Docker Runtime, existing Python Auto-Hook, Python Zero-code, and Node.js Zero-code deployment guidance.

## Deployment modes

Senda-Argus Hooks supports three main deployment patterns:

```text
1. Hook-enabled Docker Runtime
2. Existing Python Agent zero-code / auto-hook
3. Existing Node.js Agent zero-code preload
```

Senda Agent Studio provides a WebUI for managing Hook-enabled Docker Runtimes.

---

## Quick installer scripts

The repository includes small Docker helper scripts for initial setup and local evaluation. They do not modify host Agent source code.

### Install

From the repository root:

```bash
./scripts/install.sh
```

This verifies Docker, builds `senda/python-agent:0.8`, and starts Agent Studio. To build the Node Runtime as well:

```bash
./scripts/install.sh --with-node
```

Build runtime images without starting Agent Studio:

```bash
./scripts/install.sh --no-studio
```

### Create a Runtime from an existing host Agent

```bash
./scripts/runtime-create.sh \
  --name test-python-agent \
  --host-path /absolute/path/to/test-agent
```

The script creates a container labeled `com.senda.agent-runtime=true`, bind-mounts the Agent directory into `/workspace`, enables dependency installation from `requirements.txt`, and connects the Runtime to Agent Studio.

Useful options:

```bash
./scripts/runtime-create.sh --help
```

### Status

```bash
./scripts/status.sh
```

### Uninstall Agent Studio

```bash
./scripts/uninstall.sh
```

By default, Runtime containers and common Runtime images are preserved. Explicit destructive cleanup requires flags:

```bash
./scripts/uninstall.sh --remove-runtimes --yes --remove-images
```

Host Agent source directories are never deleted by these scripts.

---

## 1. Hook-enabled Docker Runtime

### Python Runtime

Build from the repository root:

```bash
docker build \
  -f docker/python/Dockerfile \
  -t senda/python-agent:0.8 \
  .
```

The Python image includes:

- Senda-Argus Hooks
- `.pth`-based startup bootstrap
- automatic supported SDK instrumentation
- environment-variable configuration
- fail-open startup behavior

### Generic host-Agent mount

Instead of building an Agent-specific image, mount an existing Agent directory into the common Runtime:

```bash
docker run --rm \
  --name my-agent \
  -v /path/to/my-agent:/workspace \
  -w /workspace \
  -e SENDA_AGENT_WORKSPACE=/workspace \
  -e SENDA_AGENT_INSTALL_DEPS=true \
  senda/python-agent:0.8 \
  python agent.py
```

When `SENDA_AGENT_INSTALL_DEPS=true`, the runtime installs `/workspace/requirements.txt` when present.

### Node Runtime

The Node Docker image preloads the Senda runtime configuration through `NODE_OPTIONS=--import=...`.

Build using:

```bash
docker build \
  -f docker/node/Dockerfile \
  -t senda/node-agent:0.9 \
  .
```

---

## 2. Senda Agent Studio

Agent Studio is intended to manage **Senda Agent Runtime** containers only.

Typical capabilities:

- Register Runtime
- Start / Stop / Restart / Delete
- Mount a host Agent directory into a common Hook-enabled image
- Live Hook events
- Agent Trace
- Docker logs

Start Agent Studio from its directory:

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8080
```

For Docker-based control, Agent Studio needs access to the Docker Engine. In the MVP this is typically provided by mounting the Docker socket.

Only containers labeled as Senda Agent Runtime should be listed and managed by Agent Studio.

---

## 3. Existing Python Environment Auto-Hook

Existing Python Agents can be instrumented without changing Agent source code.

Install the package into the same Python environment that runs the Agent:

```bash
python -m pip install ./python
```

Enable startup instrumentation:

```bash
senda-hooks autohook install
```

Status:

```bash
senda-hooks autohook status
```

Uninstall the startup hook:

```bash
senda-hooks autohook uninstall
```

The installer creates a managed startup file:

```text
senda_argus_autohook.pth
```

Python startup invokes:

```python
senda_argus_hooks.autohook.bootstrap()
```

No `import senda_argus_hooks` or `register(...)` change is required in Agent source.

### Scopes

```bash
senda-hooks autohook install --scope user
sudo senda-hooks autohook install --scope system
senda-hooks autohook install --target /path/to/site-packages
```

Inside a virtual environment, the default `auto` scope installs into that environment.

---

## 4. Python Zero-code Deployment Tool

For host-wide discovery and managed installation, use:

```bash
python3 tools/senda_argus_zero_install.py scan
```

The scanner can detect:

- Python on `PATH`
- Linux `/proc` Python processes
- `VIRTUAL_ENV`
- common venv locations under `/opt`, `/srv`, `/app`, `/var/www`, `/home`

Install into one Agent environment:

```bash
sudo python3 tools/senda_argus_zero_install.py install \
  --python /opt/my-agent/.venv/bin/python \
  --endpoint https://argus.example.local \
  --project my-agent \
  --environment prod \
  --exporters argus
```

Prefer an API-key file instead of putting secrets directly on the command line:

```bash
sudo python3 tools/senda_argus_zero_install.py install \
  --python /opt/my-agent/.venv/bin/python \
  --endpoint https://argus.example.local \
  --api-key-file /root/argus-api-key \
  --project my-agent \
  --environment prod \
  --exporters argus
```

Bulk install:

```bash
sudo python3 tools/senda_argus_zero_install.py install \
  --all --yes \
  --scan-root /opt \
  --scan-root /srv \
  --endpoint https://argus.example.local \
  --api-key-file /root/argus-api-key \
  --project production-agents \
  --environment prod \
  --exporters argus
```

Status:

```bash
python3 tools/senda_argus_zero_install.py status \
  --python /opt/my-agent/.venv/bin/python
```

Uninstall:

```bash
sudo python3 tools/senda_argus_zero_install.py uninstall \
  --python /opt/my-agent/.venv/bin/python
```

Add `--remove-sdk` to remove the installed SDK package as well.

### Configuration file

Root deployments normally use:

```text
/etc/senda-argus/hooks.env
```

User deployments normally use:

```text
~/.config/senda-argus/hooks.env
```

Already-running Python processes must be restarted before the `.pth` startup hook becomes active.

---

## 5. Node.js Zero-code Deployment

Node.js Agents can be instrumented without adding Senda imports or `register()` calls to the application source.

Recommended runtime:

- Node.js 20 / 22 LTS
- Node.js 18.19+ supported for the ESM loader path
- Linux is the primary target for process discovery and systemd injection

### Supported automatic interception

- OpenAI SDK: ESM and CommonJS
- Anthropic SDK: ESM and CommonJS
- Ollama JS: ESM and CommonJS
- MCP JS `Client`: ESM and CommonJS
- OpenAI Agents JS tracing: ESM and CommonJS

Framework-specific object/callback instrumentation is not claimed as fully automatic for LangChain, LangGraph, LlamaIndex, and Vercel AI SDK; underlying supported provider / MCP calls remain observable when they pass through supported SDKs.

### Discover Agents

```bash
node tools/senda_argus_zero_install_node.mjs scan
```

### Install into a systemd-managed Agent

```bash
sudo node tools/senda_argus_zero_install_node.mjs install \
  --service my-agent.service \
  --exporter argus \
  --endpoint https://argus.example.local \
  --api-key-file /root/senda-argus-api-key \
  --project my-agent \
  --environment prod
```

The installer:

1. installs the Node SDK runtime under `/opt/senda-argus/node`;
2. writes a protected environment file;
3. creates a systemd drop-in;
4. injects `NODE_OPTIONS=--import=...`;
5. keeps application source unchanged.

### Install by PID

```bash
sudo node tools/senda_argus_zero_install_node.mjs install \
  --pid 12345 \
  --exporter argus \
  --endpoint https://argus.example.local
```

### Shell-started Agents

```bash
node tools/senda_argus_zero_install_node.mjs install \
  --profile \
  --exporter jsonl \
  --jsonl-path /var/log/senda-argus/events.jsonl
```

### Status and removal

```bash
node tools/senda_argus_zero_install_node.mjs status
sudo node tools/senda_argus_zero_install_node.mjs uninstall
```

Full removal:

```bash
sudo node tools/senda_argus_zero_install_node.mjs uninstall \
  --remove-runtime \
  --remove-config
```

A running Node process must be restarted before an injected preload becomes active.

---

## 6. Common runtime configuration

Typical configuration:

```text
SENDA_ARGUS_ENABLED=true
SENDA_ARGUS_EXPORTER=argus
SENDA_ARGUS_ENDPOINT=https://argus.example.local
SENDA_ARGUS_API_KEY=...
SENDA_ARGUS_PROJECT=my-agent
SENDA_ARGUS_ENVIRONMENT=prod
SENDA_ARGUS_CAPTURE_PROMPT=false
SENDA_ARGUS_CAPTURE_RESPONSE=false
SENDA_ARGUS_CAPTURE_ARGUMENTS=false
SENDA_ARGUS_CAPTURE_RESULT=false
SENDA_ARGUS_CAPTURE_HASH=true
SENDA_ARGUS_REDACT=true
```

The Argus HTTP exporter posts normalized events to:

```text
<endpoint>/v1/agent-runs/ingest
```

with optional `X-API-Key` and fail-open delivery behavior.

---

## 7. Security notes

- Keep raw prompt/response capture disabled unless required.
- Keep redaction enabled.
- Store API keys in protected files or secret-management systems.
- Treat exported traces and runtime logs as security-sensitive data.
- Do not commit runtime logs or credentials to Git.
- Access to Docker Engine control is highly privileged; restrict Agent Studio deployment accordingly.

---

## 8. Troubleshooting

### Python Auto-Hook does not load

Check:

- the exact Python environment used by the Agent
- `.pth` presence via `senda-hooks autohook status`
- whether Python is started with `-S`
- whether an embedded runtime disables `site`

### Ollama package missing inside Docker Runtime

Ensure the mounted Agent directory contains:

```text
requirements.txt
```

and the Runtime has:

```text
SENDA_AGENT_INSTALL_DEPS=true
```

### Node preload does not appear active

Confirm the process was restarted after installation and that `NODE_OPTIONS` contains the managed Senda preload.

