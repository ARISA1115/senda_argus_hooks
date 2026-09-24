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

COPY docker/python/senda_argus_bootstrap.py /opt/senda/senda_argus_bootstrap.py
COPY docker/python/sitecustomize.py /opt/senda/sitecustomize.py
COPY docker/python/entrypoint.sh /usr/local/bin/senda-entrypoint

# Use a .pth startup hook instead of relying only on sitecustomize. This coexists
# with applications that already provide their own sitecustomize.py.
RUN SITE_DIR="$(python -c 'import site; print(site.getsitepackages()[0])')" && \
    printf '/opt/senda\nimport senda_argus_bootstrap; senda_argus_bootstrap.bootstrap()\n' > "${SITE_DIR}/senda_argus_autohook.pth" && \
    mkdir -p /app /var/log/senda-argus && chmod 0777 /var/log/senda-argus
WORKDIR /app

# /opt/senda is added by senda_argus_autohook.pth during Python startup.
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
    SENDA_ARGUS_REDACT=true

VOLUME ["/var/log/senda-argus"]
ENTRYPOINT ["/usr/local/bin/senda-entrypoint"]
CMD ["python", "-c", "print('Senda-Argus Python runtime ready')"]
