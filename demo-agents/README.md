# Multi-Agent demo workers

These three deterministic workers are intentionally simple. They exist to test Agent Studio's one-shot Agent run contract and multi-Agent workflow orchestration without external services.

Each worker reads JSON from `SENDA_AGENT_INPUT` and emits one structured result line:

```text
[senda-agent-result] {"...":"..."}
```

Register each directory as a Python Runtime with `restart=no`, then add useful Agent metadata:

| Agent | Description | Capabilities |
|---|---|---|
| `asset-discovery-agent` | Discover target services | `asset-discovery, service-discovery` |
| `vulnerability-agent` | Analyze service findings for vulnerabilities | `vulnerability-analysis, cve-analysis` |
| `report-agent` | Produce a final report | `report, summarization` |

For an offline smoke test, create a Workflow with planner mode `deterministic`. For real LLM routing, configure Agent Studio's `SENDA_STUDIO_LLM_BASE_URL`, `SENDA_STUDIO_LLM_MODEL`, and optionally `SENDA_STUDIO_LLM_API_KEY`, then choose planner mode `llm`.
