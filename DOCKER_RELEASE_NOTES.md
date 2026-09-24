# Docker Runtime Addition

## Added

- `docker/python/Dockerfile`
  - Senda-Argus Hooks preinstalled.
  - `.pth`-based Python startup bootstrap.
  - Automatic Python SDK instrumentation without adding `register()` to Agent startup code.
  - Environment-variable configuration for exporters and capture policy.
  - Fail-open startup behavior.
- `docker/node/Dockerfile`
  - Senda-Argus JS SDK prebuilt into the image.
  - `NODE_OPTIONS=--import=...` preload for automatic runtime configuration.
  - Current instance-level JS instrumentation limitation documented explicitly.
- `docker/compose.example.yml`
- `docker/examples/python-agent/`
- `docker/tests/smoke_python_autohook.sh`
- `docker/README.md`

## Validation

- Python auto-hook smoke test: PASS
- Python tests: 61 passed, 2 skipped
- Node tests: 10 passed
- Shell entrypoint syntax: PASS
- Node preload syntax: PASS

Docker/Podman itself was not available in the build environment, so image construction was not executed here.
