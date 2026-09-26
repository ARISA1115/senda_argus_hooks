#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE_FILE="$ROOT_DIR/agent-studio/compose.yml"

echo "== Docker =="
docker version --format 'Client {{.Client.Version}} / Server {{.Server.Version}}' 2>/dev/null || docker version

echo
echo "== Senda runtime images =="
docker images --format '{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}' | awk '$1 ~ /^senda\/(python-agent|node-agent):/' || true

echo
echo "== Senda Agent Runtime containers =="
docker ps -a \
  --filter 'label=com.senda.agent-runtime=true' \
  --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' || true

if [ -f "$COMPOSE_FILE" ]; then
  echo
echo "== Agent Studio =="
  docker compose -f "$COMPOSE_FILE" ps || true
fi
