# Shared by the Python and Node Runtime entrypoints. Sourced, not executed.

senda_prepare_log_dir() {
    mkdir -p "${SENDA_ARGUS_LOG_DIR:-/var/log/senda-argus}"
}

# Prints one SHA-256 over the given files, read in the given order.
senda_files_digest() {
    cat "$@" | sha256sum | awk '{print $1}'
}

# senda_install_once MARKER DIGEST COMMAND [ARGS...]
# Runs COMMAND only when DIGEST differs from the one recorded in MARKER, and records DIGEST
# after COMMAND succeeds. The marker lives in the container layer, so restarting the same
# container skips an unchanged install, while a new container installs again.
senda_install_once() {
    marker="$1"
    digest="$2"
    shift 2
    previous=""
    if [ -f "$marker" ]; then
        previous="$(cat "$marker" 2>/dev/null || true)"
    fi
    if [ "$digest" = "$previous" ]; then
        return 0
    fi
    "$@"
    printf '%s' "$digest" > "$marker"
}
