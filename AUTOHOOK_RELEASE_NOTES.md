# Existing Python Environment Auto-Hook

Added zero-code startup instrumentation for existing Python Agent environments.

## Added

- `senda_argus_hooks.autohook.bootstrap()` reusable startup bootstrap
- `.pth` based Python startup injection
- `senda-hooks autohook install`
- `senda-hooks autohook status`
- `senda-hooks autohook uninstall`
- virtualenv-aware automatic installation scope
- explicit `--scope user|system` and `--target` modes
- fail-open bootstrap behavior
- environment-variable based runtime configuration
- safe user-writable default JSONL location
- tests for install/status/uninstall and startup bootstrap

This implementation reuses the same startup design developed for the Docker runtime and is intended to make existing Agents observable without Agent source modification.
