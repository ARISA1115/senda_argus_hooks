#!/bin/sh
set -eu

. /usr/local/lib/senda/runtime-lib.sh

senda_prepare_log_dir

workspace="${SENDA_AGENT_WORKSPACE:-/workspace}"
install_deps="${SENDA_AGENT_INSTALL_DEPS:-false}"
requirements="${SENDA_AGENT_REQUIREMENTS:-${workspace}/requirements.txt}"

install_requirements() {
    echo "[senda-runtime] installing dependencies from $requirements" >&2
    python -m pip install --no-cache-dir -r "$requirements"
}

# Generic Runtime mode: when a host Agent directory is mounted into /workspace,
# dependencies can be installed automatically without building an Agent-specific image.
if [ "$install_deps" = "true" ] && [ -f "$requirements" ]; then
    senda_install_once /opt/senda/.requirements.sha256 "$(senda_files_digest "$requirements")" install_requirements
fi

exec "$@"
