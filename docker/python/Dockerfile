ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

LABEL org.opencontainers.image.title="Senda-Argus Python Agent Runtime" \
      org.opencontainers.image.description="Python runtime with Senda-Argus Hooks automatically loaded at interpreter startup"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/senda/source
COPY python/ /opt/senda/source/python/
RUN python -m pip install --no-cache-dir /opt/senda/source/python

COPY docker/common/runtime-lib.sh /usr/local/lib/senda/runtime-lib.sh
COPY docker/python/entrypoint.sh /usr/local/bin/senda-entrypoint

# Install the SDK's .pth startup hook, the same file the zero-code installer writes. A .pth
# hook coexists with applications that already provide their own sitecustomize.py.
RUN python -c "from senda_argus_hooks.autoinstall import install; install(scope='system')" && \
    mkdir -p /app /var/log/senda-argus && chmod 0777 /var/log/senda-argus
WORKDIR /app

ENV SENDA_ARGUS_ENABLED=true \
    SENDA_ARGUS_PROJECT=default \
    SENDA_ARGUS_ENVIRONMENT=prod \
    SENDA_ARGUS_EXPORTER=jsonl \
    SENDA_ARGUS_JSONL_PATH=/var/log/senda-argus/events.jsonl \
    SENDA_ARGUS_ENDPOINT=http://senda-argus:8000 \
    SENDA_ARGUS_CAPTURE_PROMPT=false \
    SENDA_ARGUS_CAPTURE_RESPONSE=false \
    SENDA_ARGUS_CAPTURE_ARGUMENTS=false \
    SENDA_ARGUS_CAPTURE_RESULT=false \
    SENDA_ARGUS_CAPTURE_HASH=true \
    SENDA_ARGUS_REDACT=true

VOLUME ["/var/log/senda-argus"]
ENTRYPOINT ["/usr/local/bin/senda-entrypoint"]
CMD ["python", "-c", "print('Senda-Argus Python runtime ready')"]
