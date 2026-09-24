#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/openai/resources/chat" "$TMP/openai/resources/responses" "$TMP/openai/resources/embeddings" "$TMP/log" "$TMP/sitepkgs"
cat > "$TMP/openai/__init__.py" <<'PY'
from . import resources
PY
cat > "$TMP/openai/resources/__init__.py" <<'PY'
from . import chat, responses, embeddings
PY
cat > "$TMP/openai/resources/chat/__init__.py" <<'PY'
from . import completions
PY
cat > "$TMP/openai/resources/chat/completions.py" <<'PY'
class Completions:
    def create(self, *args, **kwargs):
        return {"ok": True, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
PY
cat > "$TMP/openai/resources/responses/__init__.py" <<'PY'
class Responses:
    def create(self, *args, **kwargs):
        return {"ok": True}
PY
cat > "$TMP/openai/resources/embeddings/__init__.py" <<'PY'
class Embeddings:
    def create(self, *args, **kwargs):
        return {"ok": True}
PY
cat > "$TMP/sitepkgs/senda_argus_autohook.pth" <<PTH
$ROOT/docker/python
import senda_argus_bootstrap; senda_argus_bootstrap.bootstrap()
PTH
PYTHONPATH="$ROOT/python/src:$TMP" \
SENDA_ARGUS_ENABLED=true \
SENDA_ARGUS_EXPORTER=jsonl \
SENDA_ARGUS_JSONL_PATH="$TMP/log/events.jsonl" \
SENDA_ARGUS_BOOTSTRAP_DEBUG=true \
TEST_SITEPKGS="$TMP/sitepkgs" \
python3 - <<'PY'
import os
import site
# Process the same .pth bootstrap mechanism used in the Docker image.
site.addsitedir(os.environ["TEST_SITEPKGS"])
from openai.resources.chat.completions import Completions
assert getattr(Completions.create, "__senda_patched__", False), "OpenAI class was not patched"
Completions().create(model="fake", messages=[{"role":"user","content":"hello"}])
PY
grep -q '"event_type": "llm.request"' "$TMP/log/events.jsonl"
echo "python auto-hook smoke test: PASS"
