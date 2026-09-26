#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_IMAGE=${SENDA_PYTHON_IMAGE:-senda/python-agent:0.8}
NODE_IMAGE=${SENDA_NODE_IMAGE:-senda/node-agent:0.9}
WITH_NODE=false
NO_STUDIO=false

usage() {
  cat <<EOF
Usage: $0 [--with-node] [--no-studio]

Builds the Senda hook-enabled Docker runtime image(s) and starts Agent Studio.

Options:
  --with-node   Also build the Node.js runtime image.
  --no-studio   Build runtime images only; do not start Agent Studio.
  -h, --help    Show this help.

Environment overrides:
  SENDA_PYTHON_IMAGE   Python runtime tag (default: senda/python-agent:0.8)
  SENDA_NODE_IMAGE     Node runtime tag (default: senda/node-agent:0.9)
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-node) WITH_NODE=true ;;
    --no-studio) NO_STUDIO=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

command -v docker >/dev/null 2>&1 || {
  echo "ERROR: docker command not found. Install/start Docker Desktop or Docker Engine first." >&2
  exit 1
}

docker info >/dev/null 2>&1 || {
  echo "ERROR: Docker Engine is not available. Start Docker Desktop/Engine first." >&2
  exit 1
}

docker compose version >/dev/null 2>&1 || {
  echo "ERROR: docker compose is not available." >&2
  exit 1
}

[ -f "$ROOT_DIR/docker/python/Dockerfile" ] || {
  echo "ERROR: $ROOT_DIR/docker/python/Dockerfile not found." >&2
  exit 1
}

echo "[1/3] Building Python runtime: $PYTHON_IMAGE"
docker build -f "$ROOT_DIR/docker/python/Dockerfile" -t "$PYTHON_IMAGE" "$ROOT_DIR"

if [ "$WITH_NODE" = true ]; then
  [ -f "$ROOT_DIR/docker/node/Dockerfile" ] || {
    echo "ERROR: $ROOT_DIR/docker/node/Dockerfile not found." >&2
    exit 1
  }
  echo "[2/3] Building Node runtime: $NODE_IMAGE"
  docker build -f "$ROOT_DIR/docker/node/Dockerfile" -t "$NODE_IMAGE" "$ROOT_DIR"
else
  echo "[2/3] Node runtime build skipped (use --with-node to enable)."
fi

if [ "$NO_STUDIO" = false ]; then
  COMPOSE_FILE="$ROOT_DIR/agent-studio/compose.yml"
  [ -f "$COMPOSE_FILE" ] || {
    echo "ERROR: $COMPOSE_FILE not found." >&2
    exit 1
  }

  echo "[3/3] Starting Senda Agent Studio"
  docker compose -f "$COMPOSE_FILE" up -d --build
  echo
  echo "Senda Agent Studio is starting."
  echo "Open: http://localhost:8080"
  echo "Status: $ROOT_DIR/scripts/status.sh"
else
  echo "[3/3] Agent Studio start skipped."
fi
