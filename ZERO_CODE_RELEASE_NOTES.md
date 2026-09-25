# Senda-Argus Hooks v0.8.0 - Python Zero-code Deployment

## Added

- Existing Python/venv discovery from PATH, Linux `/proc`, `VIRTUAL_ENV`, and configurable scan roots.
- Offline wheel-based installation into detected Agent environments.
- Automatic `.pth` startup Hook installation without Agent source changes.
- Global/user `hooks.env` configuration automatically loaded at Python startup.
- Bulk `--all --yes` install/uninstall mode.
- Running PID reporting to identify Agents that require restart.
- `status` and rollback/uninstall support.
- API-key file input to avoid exposing secrets in normal command history.
- Fail-open bootstrap retained.

## Compatibility

Python 3.10+.
