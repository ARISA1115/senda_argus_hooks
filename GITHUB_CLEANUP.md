# GitHub Documentation Cleanup

Recommended root-level documentation after consolidation:

```text
README.md
DEPLOYMENT.md
CHANGELOG.md
LICENSE
```

Keep component-specific README files where they are useful:

```text
python/README.md
js/README.md
browser/README.md
docker/README.md
agent-studio/README.md
```

The following old root-level documents can be removed after reviewing the consolidated files:

```text
AUTOHOOK_RELEASE_NOTES.md
DOCKER_RELEASE_NOTES.md
NODE_ZERO_CODE_RELEASE_NOTES.md
NODE_ZERO_CODE.md
PYTHON_AUTOHOOK.md
RELEASE_NOTES_v0.6.0.md
RELEASE_NOTES_v0.7.0.md
ZERO_CODE_PYTHON.md
ZERO_CODE_RELEASE_NOTES.md
```

Suggested Git commands:

```bash
cp README.md README.md.backup

# Replace/add consolidated docs
cp /path/to/new/README.md ./README.md
cp /path/to/new/DEPLOYMENT.md ./DEPLOYMENT.md
cp /path/to/new/CHANGELOG.md ./CHANGELOG.md

# Remove superseded root docs
git rm AUTOHOOK_RELEASE_NOTES.md \
       DOCKER_RELEASE_NOTES.md \
       NODE_ZERO_CODE_RELEASE_NOTES.md \
       NODE_ZERO_CODE.md \
       PYTHON_AUTOHOOK.md \
       RELEASE_NOTES_v0.6.0.md \
       RELEASE_NOTES_v0.7.0.md \
       ZERO_CODE_PYTHON.md \
       ZERO_CODE_RELEASE_NOTES.md

git add README.md DEPLOYMENT.md CHANGELOG.md
git status
git diff --cached
```

Then commit only after reviewing links and version references.

## Recommended retained helper scripts

Keep these at repository root under `scripts/`:

```text
scripts/install.sh
scripts/status.sh
scripts/runtime-create.sh
scripts/uninstall.sh
```

They provide a small operational entry point without adding more README files.
