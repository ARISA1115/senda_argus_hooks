#!/bin/sh
set -eu

NAME=""
HOST_PATH=""
RUNTIME="python"
IMAGE="senda/python-agent:0.8"
CONTAINER_PATH="/workspace"
ENTRYPOINT="agent.py"
PROJECT="default"
ENVIRONMENT="prod"
STUDIO_ENDPOINT="http://senda-agent-studio:8080"
INSTALL_DEPS="true"
NETWORK="senda-agent-net"
RESTART_POLICY="unless-stopped"
MAXIMUM_RETRY_COUNT="0"
START=true

usage() {
  cat <<EOF
Usage: $0 --name NAME --host-path PATH [options]

Creates a Senda Agent Runtime container from the common hook-enabled runtime image.
The host Agent source is bind-mounted; no Agent-specific Docker image is required.

Required:
  --name NAME             Runtime/container name and agent_id.
  --host-path PATH        Existing host Agent directory.

Options:
  --runtime python|node   Runtime type (default: python).
  --image IMAGE           Runtime image (default: senda/python-agent:0.8).
  --container-path PATH   Mount target (default: /workspace).
  --entrypoint FILE       Python entrypoint (default: agent.py).
  --project NAME          Project label (default: default).
  --environment NAME      Environment label (default: prod).
  --studio-endpoint URL   Hook ingest endpoint (default: http://senda-agent-studio:8080).
  --no-install-deps       Do not auto-install requirements.txt.
  --network NAME          Docker network (default: senda-agent-net).
  --restart-policy POLICY Docker restart policy: no|on-failure|always|unless-stopped (default: unless-stopped).
  --maximum-retry-count N Retry count for on-failure (default: 0 = unlimited).
  --no-start              Create only; do not start.
  -h, --help              Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --name) NAME=$2; shift ;;
    --host-path) HOST_PATH=$2; shift ;;
    --runtime) RUNTIME=$2; shift ;;
    --image) IMAGE=$2; shift ;;
    --container-path) CONTAINER_PATH=$2; shift ;;
    --entrypoint) ENTRYPOINT=$2; shift ;;
    --project) PROJECT=$2; shift ;;
    --environment) ENVIRONMENT=$2; shift ;;
    --studio-endpoint) STUDIO_ENDPOINT=$2; shift ;;
    --no-install-deps) INSTALL_DEPS=false ;;
    --network) NETWORK=$2; shift ;;
    --restart-policy) RESTART_POLICY=$2; shift ;;
    --maximum-retry-count) MAXIMUM_RETRY_COUNT=$2; shift ;;
    --no-start) START=false ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[ -n "$NAME" ] || { echo "ERROR: --name is required." >&2; exit 2; }
[ -n "$HOST_PATH" ] || { echo "ERROR: --host-path is required." >&2; exit 2; }
[ -d "$HOST_PATH" ] || { echo "ERROR: host path does not exist: $HOST_PATH" >&2; exit 1; }

case "$RUNTIME" in
  python|node) ;;
  *) echo "ERROR: --runtime must be python or node." >&2; exit 2 ;;
esac

case "$RESTART_POLICY" in
  no|on-failure|always|unless-stopped) ;;
  *) echo "ERROR: --restart-policy must be no, on-failure, always, or unless-stopped." >&2; exit 2 ;;
esac
case "$MAXIMUM_RETRY_COUNT" in
  ''|*[!0-9]*) echo "ERROR: --maximum-retry-count must be a non-negative integer." >&2; exit 2 ;;
esac

if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "ERROR: container already exists: $NAME" >&2
  exit 1
fi

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "Creating Docker network: $NETWORK"
  docker network create "$NETWORK" >/dev/null
fi

if [ "$RUNTIME" = "python" ]; then
  CMD="python $ENTRYPOINT"
else
  CMD="node $ENTRYPOINT"
fi

echo "Creating Senda Agent Runtime: $NAME"
RESTART_ARGS="--restart $RESTART_POLICY"
if [ "$RESTART_POLICY" = "on-failure" ] && [ "$MAXIMUM_RETRY_COUNT" -gt 0 ]; then
  RESTART_ARGS="--restart on-failure:$MAXIMUM_RETRY_COUNT"
fi
# shellcheck disable=SC2086
docker create \
  --name "$NAME" \
  --network "$NETWORK" \
  $RESTART_ARGS \
  --label com.senda.agent-runtime=true \
  --label "com.senda.agent-id=$NAME" \
  --label "com.senda.runtime=$RUNTIME" \
  --label "com.senda.project=$PROJECT" \
  --label "com.senda.environment=$ENVIRONMENT" \
  --label "com.senda.agent.restart-policy=$RESTART_POLICY" \
  --label "com.senda.agent.maximum-retry-count=$MAXIMUM_RETRY_COUNT" \
  -v "$HOST_PATH:$CONTAINER_PATH:rw" \
  -w "$CONTAINER_PATH" \
  -e SENDA_ARGUS_ENABLED=true \
  -e SENDA_ARGUS_EXPORTER=argus \
  -e "SENDA_ARGUS_ENDPOINT=$STUDIO_ENDPOINT" \
  -e "SENDA_ARGUS_AGENT_ID=$NAME" \
  -e "SENDA_ARGUS_PROJECT=$PROJECT" \
  -e "SENDA_ARGUS_ENVIRONMENT=$ENVIRONMENT" \
  -e "SENDA_AGENT_WORKSPACE=$CONTAINER_PATH" \
  -e "SENDA_AGENT_INSTALL_DEPS=$INSTALL_DEPS" \
  "$IMAGE" \
  sh -lc "$CMD" >/dev/null

if [ "$START" = true ]; then
  docker start "$NAME" >/dev/null
  echo "Started: $NAME"
else
  echo "Created but not started: $NAME"
fi

echo "Host source: $HOST_PATH -> $CONTAINER_PATH"
echo "Image: $IMAGE"
echo "Restart policy: $RESTART_POLICY"
echo "Logs: docker logs -f $NAME"
