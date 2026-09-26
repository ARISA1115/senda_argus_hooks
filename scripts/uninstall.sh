#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE_FILE="$ROOT_DIR/agent-studio/compose.yml"
REMOVE_RUNTIMES=false
REMOVE_IMAGES=false
YES=false

usage() {
  cat <<EOF
Usage: $0 [--remove-runtimes] [--remove-images] [--yes]

Stops/removes Agent Studio. Senda Agent Runtime containers and images are kept by default.

Options:
  --remove-runtimes  Remove containers labeled com.senda.agent-runtime=true.
  --remove-images    Remove senda/python-agent and senda/node-agent images if possible.
  --yes              Required with --remove-runtimes.
  -h, --help         Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --remove-runtimes) REMOVE_RUNTIMES=true ;;
    --remove-images) REMOVE_IMAGES=true ;;
    --yes) YES=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ -f "$COMPOSE_FILE" ]; then
  echo "Stopping Agent Studio..."
  docker compose -f "$COMPOSE_FILE" down
fi

if [ "$REMOVE_RUNTIMES" = true ]; then
  [ "$YES" = true ] || {
    echo "ERROR: --remove-runtimes requires --yes." >&2
    exit 2
  }
  ids=$(docker ps -aq --filter 'label=com.senda.agent-runtime=true')
  if [ -n "$ids" ]; then
    echo "Removing Senda Agent Runtime containers..."
    docker rm -f $ids
  else
    echo "No Senda Agent Runtime containers found."
  fi
fi

if [ "$REMOVE_IMAGES" = true ]; then
  echo "Removing Senda runtime images when unused..."
  docker image rm senda/python-agent:0.8 2>/dev/null || true
  docker image rm senda/node-agent:0.9 2>/dev/null || true
fi

echo "Done. Host Agent source directories were not modified."
