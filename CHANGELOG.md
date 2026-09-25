# Changelog

This file consolidates the previous root-level release-note documents.

## Node Zero-code v0.9.0

### Added

- Node Zero-code bootstrap and ESM loader
- Automatic ESM interception for OpenAI, Anthropic, Ollama, MCP Client, and OpenAI Agents
- CommonJS eager preload/cache interception for the same provider layer
- Linux `/proc` Node process discovery
- systemd service discovery from cgroups
- Node binary and AI project discovery
- systemd `NODE_OPTIONS --import` drop-in installer
- shell-profile injection mode
- install / scan / status / uninstall workflow
- centralized Node SDK runtime installation
- protected external configuration support
- direct Argus HTTP exporter (`/v1/agent-runs/ingest`)
- fail-open network delivery
- exporter flush on normal process exit

### Compatibility

- Node 20 / 22 LTS recommended
- Node 18.19+ supported for the ESM loader path

### Verification

- existing JS test suite: 10/10 passed
- ESM zero-code OpenAI interception: passed
- CommonJS zero-code OpenAI interception: passed
- packed npm package installation into a clean central runtime: passed
- installer install -> status -> uninstall: passed
- local Argus HTTP exporter test: 2/2 events delivered

---

## Python Zero-code v0.8.0

### Added

- Existing Python/venv discovery from PATH, Linux `/proc`, `VIRTUAL_ENV`, and configurable scan roots
- Offline wheel-based installation into detected Agent environments
- Automatic `.pth` startup hook installation without Agent source changes
- Global/user `hooks.env` configuration loaded at Python startup
- bulk `--all --yes` install/uninstall mode
- running PID reporting for restart-required Agents
- status and rollback/uninstall support
- API-key file input
- fail-open bootstrap retained

### Compatibility

- Python 3.10+

---

## Existing Python Auto-Hook

### Added

- reusable `senda_argus_hooks.autohook.bootstrap()`
- `.pth`-based Python startup injection
- `senda-hooks autohook install`
- `senda-hooks autohook status`
- `senda-hooks autohook uninstall`
- virtualenv-aware automatic installation scope
- explicit `--scope user|system` and `--target` modes
- fail-open bootstrap behavior
- environment-variable runtime configuration
- safe user-writable default JSONL location
- install/status/uninstall and startup bootstrap tests

---

## Docker Runtime Addition

### Added

- `docker/python/Dockerfile`
  - Senda-Argus Hooks preinstalled
  - `.pth`-based Python startup bootstrap
  - automatic Python SDK instrumentation without Agent startup `register()` changes
  - environment-variable exporter and capture-policy configuration
  - fail-open startup behavior
- `docker/node/Dockerfile`
  - prebuilt Senda-Argus JS SDK
  - `NODE_OPTIONS=--import=...` preload
- Docker Compose example
- Python Agent example
- auto-hook smoke test

### Validation

- Python auto-hook smoke test: PASS
- Python tests: 61 passed, 2 skipped
- Node tests: 10 passed
- shell entrypoint syntax: PASS
- Node preload syntax: PASS

---

## v0.7.0

### Summary

Expanded the Node.js / TypeScript package from provider SDK hooks into agent-framework and AI-runtime integrations while preserving `schema_version = 0.2`.

### Added

- LangChain JS callback handler and lifecycle normalization
- LangGraph invoke / stream wrappers and best-effort graph instrumentation
- LlamaIndex TS retriever, embedding, and query-engine instrumentation
- Vercel AI SDK middleware instrumentation
- OpenAI Agents SDK JS/TS trace processor integration

### Final fixes

- OpenAI Agents processor updated to current trace/span lifecycle
- stable `trace_id` preservation across MCP, LangChain, LangGraph, and LlamaIndex operations
- Python-style exporter configuration accepted by JS `register()`
- MCP optional peer dependency aligned with tested package

### Verification

- TypeScript compilation passed
- JavaScript tests: 10 passed, 0 failed
- packed-package install/import smoke test passed
- Python regression tests: 61 passed, 2 skipped

---

## v0.6.0

### Added

Senda-Argus Hooks JS v0.1 with:

- AsyncLocalStorage context
- event schema 0.2
- SHA-256 hashing
- redaction
- JSONL and stdout exporters
- OpenAI hooks: `responses.create`, `chat.completions.create`, `embeddings.create`
- Anthropic hook: `messages.create`
- Ollama hook: `chat`
- MCP hook: `Client.callTool`

### Repository restructure

- Existing Python implementation moved under `python/`
- Node.js / TypeScript hooks added under `js/`
- Browser package preserved at repository root
- Python import remained `senda_argus_hooks`
- CLI entry points remained `senda-hooks` and `senda-argus`

