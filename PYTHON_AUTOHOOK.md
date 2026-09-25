# Senda-Argus Hooks - Existing Python Environment Auto-Hook

This deployment mode enables Senda-Argus Hooks in an existing Python runtime without modifying the Agent source code.

## 1. Install the package into the Agent's Python environment

Use the same Python interpreter/virtual environment that runs the Agent:

```bash
python -m pip install ./python
```

For a packaged wheel:

```bash
python -m pip install senda_argus_hooks-*.whl
```

## 2. Enable automatic startup instrumentation

```bash
senda-hooks autohook install
```

`--scope auto` is the default:

- inside a virtual environment: installs into that environment's `site-packages`
- outside a virtual environment: installs into the current user's `site-packages`

Explicit alternatives:

```bash
senda-hooks autohook install --scope user
sudo senda-hooks autohook install --scope system
senda-hooks autohook install --target /path/to/site-packages
```

The installer creates only this startup file:

```text
senda_argus_autohook.pth
```

Python's `site` initialization executes the `.pth` entry before the Agent application starts. The entry calls:

```python
senda_argus_hooks.autohook.bootstrap()
```

The Agent does not need `import senda_argus_hooks` or `register(...)` changes.

## 3. Configure with environment variables

Example for direct Senda-Argus delivery:

```bash
export SENDA_ARGUS_ENABLED=true
export SENDA_ARGUS_EXPORTER=argus
export SENDA_ARGUS_ENDPOINT=http://argus.example.local:8000
export SENDA_ARGUS_API_KEY='***'
export SENDA_ARGUS_PROJECT=my-agent
export SENDA_ARGUS_ENVIRONMENT=prod
python agent.py
```

Example for local JSONL verification:

```bash
export SENDA_ARGUS_EXPORTER=jsonl
export SENDA_ARGUS_JSONL_PATH=/tmp/senda-events.jsonl
export SENDA_ARGUS_BOOTSTRAP_DEBUG=true
python agent.py
```

If `SENDA_ARGUS_JSONL_PATH` is not set, events are written under the current user's state directory, normally:

```text
~/.local/state/senda-argus/events.jsonl
```

## 4. Status

```bash
senda-hooks autohook status
```

The command reports the Python executable, virtual-environment state, and discovered `.pth` files.

## 5. Temporarily disable without uninstalling

```bash
export SENDA_ARGUS_ENABLED=false
python agent.py
```

The `.pth` file remains installed but instrumentation is skipped.

## 6. Uninstall

```bash
senda-hooks autohook uninstall
```

Only the Senda-managed `.pth` file is removed. The Python package itself remains installed.

To remove the package too:

```bash
python -m pip uninstall senda-argus-hooks
```

## Supported automatic instrumentors

The startup hook calls `register(auto_instrument=True)` and attempts to instrument installed supported SDKs. Missing optional SDKs are ignored.

- OpenAI Python SDK
- Anthropic Python SDK
- LiteLLM
- Ollama Python SDK
- MCP Python SDK
- Senda Argus SDK
- OpenAI Agents SDK

The hook is fail-open. Instrumentation/bootstrap errors must not prevent the Agent process from starting.

## Operational notes

- Install the package into the exact Python environment used by the Agent/service.
- For systemd, Supervisor, Kubernetes, or shell services, provide `SENDA_ARGUS_*` variables in the service environment.
- Keep `SENDA_ARGUS_CAPTURE_PROMPT=false` and `SENDA_ARGUS_CAPTURE_RESPONSE=false` unless raw content capture is explicitly required.
- `SENDA_ARGUS_REDACT=true` is enabled by default.
- `python -S` disables Python's `site` initialization and therefore disables `.pth` auto-hook loading.
- Some embedded Python runtimes may disable `site`; those require an explicit bootstrap or launcher-level injection.
