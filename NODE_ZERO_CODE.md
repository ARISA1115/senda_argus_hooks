# Senda-Argus Node.js Zero-code Deployment

This release adds source-code-free instrumentation for existing Node.js AI agents.
The agent does not need to import `@senda/argus-hooks`, call `register()`, or emit audit events.

## Supported runtime

- Node.js 20 / 22 LTS recommended
- Node.js 18.19+ supported for the ESM loader registration path
- Linux is the primary target for process discovery and systemd injection

## Zero-code coverage

Automatically intercepted when loaded by an existing application:

- OpenAI SDK: ESM and CommonJS
- Anthropic SDK: ESM and CommonJS
- Ollama JS: ESM and CommonJS
- MCP JS `Client`: ESM and CommonJS
- OpenAI Agents JS tracing: ESM and CommonJS

Framework integrations that require application-owned object instances or callback registration are not claimed as fully automatic in this release:

- LangChain callbacks
- LangGraph graph-instance wrappers
- LlamaIndex component-instance instrumentation
- Vercel AI SDK middleware

Even in those frameworks, underlying OpenAI / Anthropic / Ollama / MCP calls are captured when they pass through the supported SDKs above.

## Architecture

```text
Existing Node Agent
       |
       | normal node startup
       v
NODE_OPTIONS=--import=<Senda preload>
       |
       +--> runtime configuration
       +--> CommonJS preload/cache hook
       +--> ESM module loader hook
       |
       +--> OpenAI / Anthropic / Ollama / MCP / OpenAI Agents
       |
       v
Senda event exporters
  - argus
  - jsonl
  - stdout
  - null
```

## 1. Discover existing Node agents

```bash
node tools/senda_argus_zero_install_node.mjs scan
```

JSON output:

```bash
node tools/senda_argus_zero_install_node.mjs scan --json
```

The scanner checks:

- running Node processes under `/proc`
- associated systemd service names where visible through cgroups
- Node binaries on PATH and common NVM locations
- projects under `/opt`, `/srv`, `/app`, `/var/www`, and the current user's home
- `package.json` dependencies related to supported AI runtimes

Additional roots can be supplied repeatedly:

```bash
node tools/senda_argus_zero_install_node.mjs scan \
  --scan-root /opt/agents \
  --scan-root /data/apps
```

## 2. Install into a systemd-managed agent

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

1. installs the packed Node SDK into `/opt/senda-argus/node`;
2. writes `/etc/senda-argus/node.env` with mode `0600`;
3. creates `/etc/systemd/system/my-agent.service.d/90-senda-argus-node.conf`;
4. injects `NODE_OPTIONS=--import=.../zerocode/preload.js`;
5. keeps the application source unchanged.

Restart is not performed unless requested:

```bash
sudo node tools/senda_argus_zero_install_node.mjs install \
  --service my-agent.service \
  --restart \
  ...
```

## 3. Install by running PID

```bash
sudo node tools/senda_argus_zero_install_node.mjs install \
  --pid 12345 \
  --exporter argus \
  --endpoint https://argus.example.local
```

If the process belongs to a detectable systemd service, its service drop-in is generated automatically.

## 4. Install into all detected Node systemd services

```bash
sudo node tools/senda_argus_zero_install_node.mjs install \
  --all --yes \
  --exporter argus \
  --endpoint https://argus.example.local \
  --api-key-file /root/senda-argus-api-key \
  --project production-agents \
  --environment prod
```

`--all` requires `--yes` as a safety guard.

## 5. Shell-started Node agents

For agents started from login shells, cron wrappers, or manual commands:

```bash
node tools/senda_argus_zero_install_node.mjs install \
  --profile \
  --exporter jsonl \
  --jsonl-path /var/log/senda-argus/events.jsonl
```

Root installation writes `/etc/profile.d/senda-argus-node.sh`.
A non-root installation writes a user profile helper and a managed source line in `~/.profile`.
A new login/session is required after profile injection.

## Configuration

Default root configuration file:

```text
/etc/senda-argus/node.env
```

Example:

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

The preload also recognizes:

- `SENDA_ARGUS_ENV_FILE`
- `/etc/senda-argus/node.env`
- `/etc/senda-argus/hooks.env`
- `~/.config/senda-argus/node.env`
- `~/.config/senda-argus/hooks.env`

Environment values already set on the process take precedence over values from the file.

## Direct Argus exporter

The Node package now includes an Argus HTTP exporter compatible with the Python exporter behavior.
It posts normalized events to:

```text
<endpoint>/v1/agent-runs/ingest
```

with optional `X-API-Key` and fail-open behavior.

## Status

```bash
node tools/senda_argus_zero_install_node.mjs status
```

or:

```bash
node tools/senda_argus_zero_install_node.mjs status --json
```

## Uninstall

Remove the Zero-code injection while preserving the SDK runtime and config:

```bash
sudo node tools/senda_argus_zero_install_node.mjs uninstall
```

Full removal:

```bash
sudo node tools/senda_argus_zero_install_node.mjs uninstall \
  --remove-runtime \
  --remove-config
```

Only files recorded in the Senda installer state are removed.
Existing agent source files are not modified.

## Dry run

```bash
sudo node tools/senda_argus_zero_install_node.mjs install \
  --service my-agent.service \
  --dry-run
```

## Operational note

A running Node process cannot have its environment retroactively changed. After installation, the process must be restarted (or naturally restarted) before the preload is active.
This does not require an application source-code change.

PM2/container-specific orchestration can still inherit the same preload through `NODE_OPTIONS`; this release automates systemd and shell-profile deployment, while the existing Senda Docker runtime already uses the same `--import` mechanism.
