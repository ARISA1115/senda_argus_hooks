#!/bin/sh
set -eu

. /usr/local/lib/senda/runtime-lib.sh

senda_prepare_log_dir

workspace="${SENDA_AGENT_WORKSPACE:-/workspace}"
install_deps="${SENDA_AGENT_INSTALL_DEPS:-false}"
manifest="${SENDA_AGENT_PACKAGE_JSON:-${workspace}/package.json}"
deps_dir="/opt/senda/node-deps"

install_node_dependencies() {
    echo "[senda-runtime] installing dependencies from $manifest" >&2
    rm -rf "$deps_dir"
    mkdir -p "$deps_dir"
    cp "$manifest" "$deps_dir/package.json"
    if [ -n "$lockfile" ]; then
        cp "$lockfile" "$deps_dir/"
        (cd "$deps_dir" && npm ci --omit=dev --no-audit --no-fund)
    else
        (cd "$deps_dir" && npm install --omit=dev --no-audit --no-fund)
    fi
}

# Generic Runtime mode: install the mounted Agent's dependencies inside the container, as the
# Python Runtime does with requirements.txt. They go to /opt/senda/node-deps and are exposed as
# /node_modules, which Node searches after the Agent's own node_modules, so the host Agent
# directory is left unchanged.
if [ "$install_deps" = "true" ] && [ -f "$manifest" ]; then
    lockfile=""
    for name in package-lock.json npm-shrinkwrap.json; do
        if [ -f "$(dirname "$manifest")/$name" ]; then
            lockfile="$(dirname "$manifest")/$name"
            break
        fi
    done
    senda_install_once /opt/senda/.node-deps.sha256 "$(senda_files_digest "$manifest" ${lockfile:+"$lockfile"})" install_node_dependencies
    ln -sfn "$deps_dir/node_modules" /node_modules
fi

exec "$@"
