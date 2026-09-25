# Senda-Argus Hooks v0.9.0 - Node Zero-code Release Notes

## Added

- Node Zero-code bootstrap and ESM loader
- automatic ESM interception for OpenAI, Anthropic, Ollama, MCP Client, and OpenAI Agents
- CommonJS eager preload/cache interception for the same provider layer
- Linux `/proc` Node process discovery
- systemd service discovery from cgroups
- Node binary and AI project discovery
- systemd `NODE_OPTIONS --import` drop-in installer
- shell-profile injection mode
- install / scan / status / uninstall workflow
- centralized Node SDK runtime installation
- protected external configuration file support
- direct Argus HTTP exporter (`/v1/agent-runs/ingest`)
- fail-open network delivery
- exporter flush on normal process exit

## Compatibility

The general JS SDK remains usable on Node >=18.
The Zero-code ESM loader path requires Node >=18.19; Node 20 or Node 22 LTS is recommended.

## Tested

- existing JS test suite: 10/10 passed
- ESM zero-code OpenAI interception: passed
- CommonJS zero-code OpenAI interception: passed
- packed npm package installation into a clean central runtime: passed
- installer runtime/config/profile install -> status -> uninstall: passed
- Argus HTTP exporter to a local `/v1/agent-runs/ingest` compatible endpoint: 2/2 events delivered

Systemd service restart behavior cannot be integration-tested in the build container because PID 1 is not systemd; generated drop-in logic is included for deployment on systemd hosts.
