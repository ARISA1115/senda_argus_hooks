#!/bin/sh
set -eu

mkdir -p "${SENDA_ARGUS_LOG_DIR:-/var/log/senda-argus}"

workspace="${SENDA_AGENT_WORKSPACE:-/workspace}"
install_deps="${SENDA_AGENT_INSTALL_DEPS:-false}"
requirements="${SENDA_AGENT_REQUIREMENTS:-${workspace}/requirements.txt}"

# Generic Runtime mode: when a host Agent directory is mounted into /workspace,
# dependencies can be installed automatically without building an Agent-specific image.
if [ "$install_deps" = "true" ] && [ -f "$requirements" ]; then
    marker="/opt/senda/.requirements.sha256"
    current="$(sha256sum "$requirements" | awk '{print $1}')"
    previous=""
    if [ -f "$marker" ]; then
        previous="$(cat "$marker" 2>/dev/null || true)"
    fi
    if [ "$current" != "$previous" ]; then
        echo "[senda-runtime] installing dependencies from $requirements" >&2
        python -m pip install --no-cache-dir -r "$requirements"
        printf '%s' "$current" > "$marker"
    fi
fi

exec "$@"
