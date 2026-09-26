"""MCP control surface for Senda Arugus Agent Studio.

Run as a stdio MCP server:
    SENDA_STUDIO_URL=http://senda-agent-studio:8080 python -m app.mcp_server

The MCP server never manipulates Docker directly.  It delegates to Agent Studio's
validated control-plane API so policy, metadata and workflow state remain
centralized.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP('senda-argus-agent-studio')
BASE = os.getenv('SENDA_STUDIO_URL', 'http://senda-agent-studio:8080').rstrip('/')


def _request(method: str, path: str, body: Any = None, timeout: float = 360) -> Any:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = {'Content-Type': 'application/json'} if data is not None else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode()) if raw else {'ok': True}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace')
        raise RuntimeError(f'Agent Studio HTTP {exc.code}: {detail}') from exc


@mcp.tool()
def list_agents() -> list[dict[str, Any]]:
    """List registered Agents and their orchestration metadata/capabilities."""
    return _request('GET', '/api/agents').get('agents', [])


@mcp.tool()
def get_agent(agent_id: str) -> dict[str, Any]:
    """Get one registered Agent, including schemas, risk and approval policy."""
    from urllib.parse import quote
    return _request('GET', '/api/agents/' + quote(agent_id, safe=''))


@mcp.tool()
def run_agent(agent_id: str, input: dict[str, Any], workflow_id: str | None = None, parent_agent_id: str | None = None, caller_id: str = 'mcp-external') -> dict[str, Any]:
    """Start a one-shot execution of a registered Agent and return its Agent run ID."""
    from urllib.parse import quote
    return _request('POST', '/api/agents/' + quote(agent_id, safe='') + '/run', {
        'input': input,
        'workflow_id': workflow_id,
        'parent_agent_id': parent_agent_id,
        'caller_id': caller_id,
    })


@mcp.tool()
def get_run(run_id: str) -> dict[str, Any]:
    """Get current status of a one-shot Agent run."""
    from urllib.parse import quote
    return _request('GET', '/api/runs/' + quote(run_id, safe=''))


@mcp.tool()
def wait_run(run_id: str, timeout: int = 300) -> dict[str, Any]:
    """Wait until an Agent run exits, then return its final container state."""
    from urllib.parse import quote
    timeout = max(1, min(int(timeout), 3600))
    return _request('POST', f'/api/runs/{quote(run_id, safe="")}/wait?timeout={timeout}', timeout=timeout + 10)


@mcp.tool()
def get_run_result(run_id: str) -> dict[str, Any]:
    """Return an Agent run's structured result marker and logs."""
    from urllib.parse import quote
    return _request('GET', '/api/runs/' + quote(run_id, safe='') + '/result')


@mcp.tool()
def start_workflow(
    goal: str,
    input: dict[str, Any] | None = None,
    entry_agent_id: str | None = None,
    allowed_agents: list[str] | None = None,
    max_steps: int = 8,
    llm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a Supervisor-controlled multi-Agent workflow in Agent Studio.

    entry_agent_id is a backward-compatible first-worker override; normally leave it unset.
    """
    return _request('POST', '/api/workflows', {
        'goal': goal,
        'input': input or {},
        'entry_agent_id': entry_agent_id,
        'allowed_agents': allowed_agents or [],
        'max_steps': max_steps,
        'llm': llm or {},
        'start_immediately': True,
    })


@mcp.tool()
def register_workflow(
    goal: str,
    input: dict[str, Any] | None = None,
    entry_agent_id: str | None = None,
    allowed_agents: list[str] | None = None,
    max_steps: int = 8,
    llm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register a Supervisor-controlled workflow without starting it."""
    return _request('POST', '/api/workflows', {
        'goal': goal,
        'input': input or {},
        'entry_agent_id': entry_agent_id,
        'allowed_agents': allowed_agents or [],
        'max_steps': max_steps,
        'llm': llm or {},
        'start_immediately': False,
    })


@mcp.tool()
def run_workflow(workflow_id: str) -> dict[str, Any]:
    """Start or re-run a registered workflow."""
    from urllib.parse import quote
    return _request('POST', '/api/workflows/' + quote(workflow_id, safe='') + '/start')


@mcp.tool()
def stop_workflow(workflow_id: str) -> dict[str, Any]:
    """Stop a running workflow and its active Agent run."""
    from urllib.parse import quote
    return _request('POST', '/api/workflows/' + quote(workflow_id, safe='') + '/stop')


@mcp.tool()
def delete_workflow(workflow_id: str) -> dict[str, Any]:
    """Delete a registered workflow definition/current run state."""
    from urllib.parse import quote
    return _request('DELETE', '/api/workflows/' + quote(workflow_id, safe=''))


@mcp.tool()
def get_workflow(workflow_id: str) -> dict[str, Any]:
    """Get workflow status and all Agent steps."""
    from urllib.parse import quote
    return _request('GET', '/api/workflows/' + quote(workflow_id, safe=''))


@mcp.tool()
def approve_workflow(workflow_id: str) -> dict[str, Any]:
    """Approve a pending high-risk/approval-required Agent step."""
    from urllib.parse import quote
    return _request('POST', '/api/workflows/' + quote(workflow_id, safe='') + '/approve')


if __name__ == '__main__':
    mcp.run(transport='stdio')
