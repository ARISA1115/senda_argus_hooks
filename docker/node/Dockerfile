ARG NODE_VERSION=22
FROM node:${NODE_VERSION}-bookworm-slim

LABEL org.opencontainers.image.title="Senda-Argus Node Agent Runtime" \
      org.opencontainers.image.description="Node.js runtime with Senda-Argus runtime preload"

WORKDIR /opt/senda/source
COPY js/ /opt/senda/source/js/
RUN cd /opt/senda/source/js && npm ci && npm run build && \
    mkdir -p /opt/senda/argus-hooks && cp -R dist package.json README.md /opt/senda/argus-hooks/

COPY docker/node/auto-hook.mjs /opt/senda/auto-hook.mjs
COPY docker/node/entrypoint.sh /usr/local/bin/senda-entrypoint
RUN mkdir -p /app /var/log/senda-argus && chmod 0777 /var/log/senda-argus

WORKDIR /app
ENV SENDA_ARGUS_ENABLED=true \
    SENDA_ARGUS_PROJECT=default \
    SENDA_ARGUS_ENVIRONMENT=prod \
    SENDA_ARGUS_EXPORTER=jsonl \
    SENDA_ARGUS_JSONL_PATH=/var/log/senda-argus/events.jsonl \
    SENDA_ARGUS_CAPTURE_PROMPT=false \
    SENDA_ARGUS_CAPTURE_RESPONSE=false \
    SENDA_ARGUS_CAPTURE_ARGUMENTS=false \
    SENDA_ARGUS_CAPTURE_RESULT=false \
    SENDA_ARGUS_CAPTURE_HASH=true \
    SENDA_ARGUS_REDACT=true \
    NODE_OPTIONS="--import=/opt/senda/auto-hook.mjs"

VOLUME ["/var/log/senda-argus"]
ENTRYPOINT ["/usr/local/bin/senda-entrypoint"]
CMD ["node", "-e", "console.log('Senda-Argus Node runtime ready')"]
