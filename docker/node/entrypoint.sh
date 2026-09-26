#!/bin/sh
set -eu
mkdir -p "${SENDA_ARGUS_LOG_DIR:-/var/log/senda-argus}"
exec "$@"
