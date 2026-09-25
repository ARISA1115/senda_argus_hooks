# Senda-Argus Hooks v0.6.0 release package

Added Senda-Argus Hooks JS v0.1 with:

- AsyncLocalStorage context
- event schema 0.2
- SHA-256 hashing
- redaction
- JSONL and stdout exporters
- OpenAI hooks: responses.create, chat.completions.create, embeddings.create
- Anthropic hook: messages.create
- Ollama hook: chat
- MCP hook: Client.callTool

The existing Python source is included unchanged except for removal of generated cache/build metadata. The main README is updated for v0.6.0.


## Repository layout

The v0.6.0 source tree separates language implementations into peer directories:

```text
senda-argus-hooks/
├─ python/
│  ├─ pyproject.toml
│  └─ src/senda_argus_hooks/
├─ js/
│  ├─ package.json
│  ├─ tsconfig.json
│  └─ src/
├─ README.md
└─ RELEASE_NOTES_v0.6.0.md
```

This avoids treating the root `src/` directory as implicitly Python-only and makes future language-specific releases easier to maintain.

## Repository restructure

- Existing Python implementation moved from repository root into `python/`.
- Python package import remains `senda_argus_hooks`; CLI entry points remain `senda-hooks` and `senda-argus`.
- Existing Python tests, examples, docs, and Langflow components moved with the Python package.
- Existing `browser/` package is preserved at repository root.
- New Node.js / TypeScript SDK hooks are under `js/`.
- Python package version updated to `0.6.0`; JS package remains `0.1.0` as the initial JS implementation bundled with repository release v0.6.0.

