from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .docker_runtime import DockerRuntime
from .event_bus import EventBus
from .orchestrator import WorkflowOrchestrator, SUPERVISOR_AGENT_ID
from .store import EventStore

BASE = Path(__file__).resolve().parent
DB = os.getenv('SENDA_STUDIO_DB', '/data/studio.db')
store = EventStore(DB)
event_bus = EventBus(store)
app = FastAPI(title='Senda Arugus Agent Studio', version='0.5.4')
logger = logging.getLogger('uvicorn.error')
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
_runtime = None
_background_tasks: set[asyncio.Task] = set()
_workflow_tasks: dict[str, asyncio.Task] = {}


def runtime():
    global _runtime
    if _runtime is None:
        _runtime = DockerRuntime()
    return _runtime


def orchestrator() -> WorkflowOrchestrator:
    return WorkflowOrchestrator(store, runtime(), event_bus)


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def spawn_workflow(workflow_id: str, approved_pending: bool = False) -> asyncio.Task:
    current = _workflow_tasks.get(workflow_id)
    if current is not None and not current.done():
        raise RuntimeError('workflow is already running')
    task = spawn(orchestrator().run(workflow_id, approved_pending=approved_pending))
    _workflow_tasks[workflow_id] = task

    def _cleanup(done: asyncio.Task) -> None:
        if _workflow_tasks.get(workflow_id) is done:
            _workflow_tasks.pop(workflow_id, None)

    task.add_done_callback(_cleanup)
    return task


class AgentCreate(BaseModel):
    name: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,63}$')
    image: str
    runtime: str = 'python'
    command: str | None = None
    host_path: str | None = None
    container_path: str = '/workspace'
    entrypoint: str | None = None
    install_dependencies: bool = True
    agent_id: str | None = None
    project: str = 'default'
    environment: str = 'prod'
    studio_endpoint: str = 'http://senda-agent-studio:8080'
    env: dict[str, str] = Field(default_factory=dict)
    restart_policy: Literal['no', 'on-failure', 'always', 'unless-stopped'] = 'unless-stopped'
    maximum_retry_count: int = Field(default=0, ge=0, le=1000)
    network: str = 'senda-agent-net'
    start_immediately: bool = True

    # Multi-Agent registry metadata.  This is intentionally stored separately
    # from Docker labels so JSON Schema can remain reasonably rich.
    description: str = ''
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal['low', 'medium', 'high'] = 'low'
    requires_approval: bool = False
    allowed_callers: list[str] = Field(default_factory=list)


class AgentProfileUpdate(BaseModel):
    description: str = ''
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal['low', 'medium', 'high'] = 'low'
    requires_approval: bool = False
    allowed_callers: list[str] = Field(default_factory=list)


class AgentRunRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    workflow_id: str | None = None
    parent_agent_id: str | None = None
    caller_id: str = 'external'
    env: dict[str, str] = Field(default_factory=dict)


class WorkflowCreate(BaseModel):
    start_immediately: bool = True
    goal: str = Field(min_length=1, max_length=12000)
    input: dict[str, Any] = Field(default_factory=dict)
    entry_agent_id: str | None = None
    allowed_agents: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=8, ge=1, le=50)
    llm: dict[str, Any] = Field(default_factory=dict)


@app.get('/')
def index():
    return FileResponse(BASE / 'static' / 'index.html')


@app.get('/api/health')
def health():
    return {
        'ok': True,
        'version': '0.5.4',
        'multi_agent': True,
        'supervisor_agent_id': SUPERVISOR_AGENT_ID,
        'supervisor_modes': ['llm', 'jev', 'deterministic'],
        'orchestrator_mode': os.getenv('SENDA_STUDIO_ORCHESTRATOR_MODE', 'llm'),
    }


# ---------------------------------------------------------------------------
# Agent registry / runtime control
# ---------------------------------------------------------------------------

@app.get('/api/agents')
def agents():
    try:
        return {'agents': store.merge_agent_profiles(runtime().list_agents())}
    except Exception as e:
        raise HTTPException(503, f'Docker unavailable: {e}')


@app.get('/api/agents/{agent_id}')
def get_agent(agent_id: str):
    try:
        rows = store.merge_agent_profiles(runtime().list_agents())
        row = next((a for a in rows if a.get('agent_id') == agent_id or a.get('name') == agent_id or a.get('container_id') == agent_id), None)
        if not row:
            raise KeyError(agent_id)
        return row
    except KeyError:
        raise HTTPException(404, 'agent not found')
    except Exception as e:
        raise HTTPException(503, f'Docker unavailable: {e}')


@app.post('/api/agents')
def create_agent(spec: AgentCreate):
    payload = spec.model_dump()
    agent_id = payload.get('agent_id') or payload['name']
    profile = {
        key: payload[key]
        for key in (
            'description', 'capabilities', 'tags', 'input_schema', 'output_schema',
            'risk_level', 'requires_approval', 'allowed_callers',
        )
    }
    try:
        created = runtime().create(payload)
        store.upsert_agent_profile(agent_id, profile)
        return {**created, **store.get_agent_profile(agent_id)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.put('/api/agents/{agent_id}/profile')
def update_agent_profile(agent_id: str, profile: AgentProfileUpdate):
    # Require the Agent to exist in the runtime registry.
    try:
        get_agent(agent_id)
    except HTTPException:
        raise
    store.upsert_agent_profile(agent_id, profile.model_dump())
    return store.get_agent_profile(agent_id)


@app.post('/api/agents/{agent_id}/run')
def run_agent(agent_id: str, req: AgentRunRequest):
    profile = store.get_agent_profile(agent_id)
    callers = set(profile.get('allowed_callers') or [])
    if callers and req.caller_id not in callers:
        raise HTTPException(403, f'caller {req.caller_id!r} is not allowed to run Agent {agent_id!r}')
    if profile.get('requires_approval'):
        raise HTTPException(409, 'Agent requires approval; start it through an Agent Studio workflow approval gate')
    run_id = 'ar_' + uuid.uuid4().hex[:20]
    try:
        return runtime().run_agent(
            agent_id,
            run_id,
            req.input,
            req.workflow_id,
            req.parent_agent_id or req.caller_id,
            req.env,
        )
    except KeyError:
        raise HTTPException(404, 'agent not found')
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post('/api/agents/{ident}/{action}')
def agent_action(ident: str, action: str):
    if action not in {'start', 'stop', 'restart'}:
        raise HTTPException(404, 'unknown action')
    try:
        getattr(runtime(), action)(ident)
        return {'ok': True, 'action': action}
    except KeyError:
        raise HTTPException(404, 'agent not found')
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete('/api/agents/{ident}')
def delete_agent(ident: str):
    try:
        row = get_agent(ident)
        runtime().remove(row['container_id'])
        store.delete_agent_profile(str(row.get('agent_id') or ident))
        return {'ok': True, 'action': 'delete'}
    except HTTPException:
        raise
    except KeyError:
        raise HTTPException(404, 'agent not found')
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get('/api/agents/{ident}/logs')
def agent_logs(ident: str, tail: int = 200):
    try:
        return {'logs': runtime().logs(ident, tail=tail), 'tail': max(1, min(tail, 2000))}
    except KeyError:
        raise HTTPException(404, 'agent not found')
    except Exception as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# One-shot Agent runs: this is the execution primitive exposed to MCP/LLM.
# ---------------------------------------------------------------------------



@app.get('/api/runs')
def list_runs(workflow_id: str | None = None, limit: int = 100):
    try:
        return {'runs': runtime().list_runs(workflow_id, limit)}
    except Exception as e:
        raise HTTPException(503, str(e))


@app.get('/api/runs/{run_id}')
def get_run(run_id: str):
    try:
        return runtime().get_run(run_id)
    except KeyError:
        raise HTTPException(404, 'run not found')
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post('/api/runs/{run_id}/wait')
def wait_run(run_id: str, timeout: float = 300):
    try:
        return runtime().wait_run(run_id, timeout=max(1, min(float(timeout), 3600)))
    except KeyError:
        raise HTTPException(404, 'run not found')
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get('/api/runs/{run_id}/result')
def run_result(run_id: str):
    try:
        return runtime().run_result(run_id)
    except KeyError:
        raise HTTPException(404, 'run not found')
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete('/api/runs/{run_id}')
def delete_run(run_id: str):
    try:
        runtime().remove_run(run_id)
        return {'ok': True}
    except KeyError:
        raise HTTPException(404, 'run not found')
    except Exception as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Multi-Agent workflows / LLM supervisor
# ---------------------------------------------------------------------------

@app.get('/api/workflows')
def workflows(limit: int = 50):
    return {'workflows': store.list_workflows(limit)}


@app.post('/api/workflows')
async def create_workflow(req: WorkflowCreate):
    _validate_workflow_agents(req.allowed_agents, req.entry_agent_id)
    wf = store.create_workflow(
        req.goal,
        req.input,
        req.max_steps,
        req.allowed_agents,
        req.entry_agent_id,
        req.llm,
        status='queued' if req.start_immediately else 'registered',
    )
    if req.start_immediately:
        try:
            spawn_workflow(wf['workflow_id'])
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
    return store.get_workflow(wf['workflow_id'])


def _validate_workflow_agents(allowed_agents: list[str], entry_agent_id: str | None) -> None:
    known = {str(a.get('agent_id')) for a in store.merge_agent_profiles(runtime().list_agents())}
    if entry_agent_id and entry_agent_id not in known:
        raise HTTPException(400, f'Unknown entry_agent_id: {entry_agent_id}')
    unknown = [a for a in allowed_agents if a not in known]
    if unknown:
        raise HTTPException(400, f'Unknown allowed_agents: {unknown}')


@app.get('/api/workflows/{workflow_id}')
def get_workflow(workflow_id: str):
    try:
        return store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(404, 'workflow not found')


@app.post('/api/workflows/{workflow_id}/start')
async def start_workflow(workflow_id: str):
    try:
        wf = store.get_workflow(workflow_id)
        current = _workflow_tasks.get(workflow_id)
        if current is not None and not current.done():
            raise HTTPException(409, 'workflow is already running')
        _validate_workflow_agents(wf.get('allowed_agents') or [], wf.get('entry_agent_id'))
        store.reset_workflow(workflow_id, status='queued')
        spawn_workflow(workflow_id)
        return store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(404, 'workflow not found')
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.post('/api/workflows/{workflow_id}/stop')
async def stop_workflow(workflow_id: str):
    try:
        wf = store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(404, 'workflow not found')

    active_run = next((
        step.get('agent_run_id') for step in reversed(wf.get('steps') or [])
        if step.get('status') == 'running' and step.get('agent_run_id')
    ), None)
    if active_run:
        try:
            await asyncio.to_thread(runtime().stop_run, active_run)
        except Exception as exc:
            logger.warning('failed to stop workflow Agent run %s: %s', active_run, exc)

    task = _workflow_tasks.get(workflow_id)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    else:
        store.update_workflow(workflow_id, status='stopped', pending_agent_id=None, pending_input=None, error=None)
        try:
            await orchestrator()._event(workflow_id, 'workflow.stopped', 'stopped')
        except Exception:
            pass
    return store.get_workflow(workflow_id)


@app.delete('/api/workflows/{workflow_id}')
async def delete_workflow(workflow_id: str):
    try:
        wf = store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(404, 'workflow not found')

    active_run = next((
        step.get('agent_run_id') for step in reversed(wf.get('steps') or [])
        if step.get('status') == 'running' and step.get('agent_run_id')
    ), None)
    if active_run:
        try:
            await asyncio.to_thread(runtime().stop_run, active_run)
        except Exception:
            pass
    task = _workflow_tasks.pop(workflow_id, None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    store.delete_workflow(workflow_id)
    return {'ok': True, 'action': 'delete', 'workflow_id': workflow_id}


@app.post('/api/workflows/{workflow_id}/approve')
async def approve_workflow(workflow_id: str):
    try:
        wf = store.get_workflow(workflow_id)
        if wf.get('status') != 'pending_approval':
            raise HTTPException(409, 'workflow is not waiting for approval')
        try:
            spawn_workflow(workflow_id, approved_pending=True)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {'ok': True, 'workflow_id': workflow_id, 'status': 'resuming'}
    except KeyError:
        raise HTTPException(404, 'workflow not found')


# ---------------------------------------------------------------------------
# Hook events / traces
# ---------------------------------------------------------------------------

@app.get('/api/events')
def events(agent_id: str | None = None, limit: int = 100):
    return {'events': store.recent(agent_id, limit)}


@app.get('/api/agents/{agent_id}/trace-groups')
def trace_groups(agent_id: str, limit: int = 30):
    return {'traces': store.trace_groups(agent_id, limit)}


@app.get('/api/traces')
def traces(trace_id: str | None = None, run_id: str | None = None, agent_id: str | None = None, limit: int = 500):
    return {'events': store.trace(trace_id, run_id, agent_id, limit)}


@app.get('/api/traces/detail')
def trace_detail(trace_id: str | None = None, run_id: str | None = None, agent_id: str | None = None, limit: int = 500):
    return store.trace_detail(trace_id, run_id, agent_id, limit)


@app.post('/v1/agent-runs/ingest')
async def ingest(req: Request):
    body = await req.json()
    evs = body.get('events', []) if isinstance(body, dict) else []
    if not isinstance(evs, list):
        raise HTTPException(400, 'events must be a list')
    accepted = await event_bus.publish([e for e in evs if isinstance(e, dict)])
    return {'accepted': accepted}


@app.get('/api/events/stream')
async def stream(request: Request, agent_id: str | None = None):
    q = event_bus.subscribe(maxsize=256)

    async def gen():
        try:
            yield 'event: ready\ndata: {}\n\n'
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'
                    continue
                if agent_id and ev.get('agent_id') != agent_id:
                    continue
                yield 'event: hook\ndata: ' + json.dumps(ev, ensure_ascii=False, default=str) + '\n\n'
        finally:
            event_bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
