from __future__ import annotations
import asyncio, json, logging, os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal
from .store import EventStore
from .docker_runtime import DockerRuntime

BASE=Path(__file__).resolve().parent
DB=os.getenv('SENDA_STUDIO_DB','/data/studio.db')
store=EventStore(DB)
subscribers:set[asyncio.Queue]=set()
app=FastAPI(title='Senda Arugus Agent Studio', version='0.3.0')
logger=logging.getLogger('uvicorn.error')
app.mount('/static', StaticFiles(directory=BASE/'static'), name='static')
_runtime=None

def runtime():
    global _runtime
    if _runtime is None: _runtime=DockerRuntime()
    return _runtime

class AgentCreate(BaseModel):
    name:str=Field(pattern=r'^[A-Za-z0-9_.-]{1,63}$')
    image:str
    runtime:str='python'
    command:str|None=None
    host_path:str|None=None
    container_path:str='/workspace'
    entrypoint:str|None=None
    install_dependencies:bool=True
    agent_id:str|None=None
    project:str='default'
    environment:str='prod'
    studio_endpoint:str='http://senda-agent-studio:8080'
    env:dict[str,str]=Field(default_factory=dict)
    restart_policy:Literal['no','on-failure','always','unless-stopped']='unless-stopped'
    maximum_retry_count:int=Field(default=0, ge=0, le=1000)
    network:str='senda-agent-net'

@app.get('/')
def index(): return FileResponse(BASE/'static'/'index.html')

@app.get('/api/health')
def health(): return {'ok':True,'version':'0.3.0'}

@app.get('/api/agents')
def agents():
    try: return {'agents':runtime().list_agents()}
    except Exception as e: raise HTTPException(503, f'Docker unavailable: {e}')

@app.post('/api/agents')
def create_agent(spec:AgentCreate):
    try: return runtime().create(spec.model_dump())
    except Exception as e: raise HTTPException(400, str(e))

@app.post('/api/agents/{ident}/{action}')
def agent_action(ident:str, action:str):
    if action not in {'start','stop','restart'}: raise HTTPException(404,'unknown action')
    try:
        getattr(runtime(),action)(ident)
        return {'ok':True,'action':action}
    except KeyError: raise HTTPException(404,'agent not found')
    except Exception as e: raise HTTPException(400,str(e))

@app.delete('/api/agents/{ident}')
def delete_agent(ident:str):
    try:
        runtime().remove(ident)
        return {'ok':True,'action':'delete'}
    except KeyError: raise HTTPException(404,'agent not found')
    except Exception as e: raise HTTPException(400,str(e))

@app.get('/api/agents/{ident}/logs')
def agent_logs(ident:str, tail:int=200):
    try:
        return {'logs': runtime().logs(ident, tail=tail), 'tail': max(1,min(tail,2000))}
    except KeyError: raise HTTPException(404,'agent not found')
    except Exception as e: raise HTTPException(400,str(e))

@app.get('/api/events')
def events(agent_id:str|None=None, limit:int=100): return {'events':store.recent(agent_id,limit)}

@app.get('/api/agents/{agent_id}/trace-groups')
def trace_groups(agent_id:str, limit:int=30): return {'traces':store.trace_groups(agent_id,limit)}

@app.get('/api/traces')
def traces(trace_id:str|None=None, run_id:str|None=None, agent_id:str|None=None, limit:int=500):
    return {'events':store.trace(trace_id,run_id,agent_id,limit)}

@app.get('/api/traces/detail')
def trace_detail(trace_id:str|None=None, run_id:str|None=None, agent_id:str|None=None, limit:int=500):
    return store.trace_detail(trace_id,run_id,agent_id,limit)

@app.post('/v1/agent-runs/ingest')
async def ingest(req:Request):
    body=await req.json(); evs=body.get('events',[]) if isinstance(body,dict) else []
    if not isinstance(evs,list): raise HTTPException(400,'events must be a list')
    accepted=store.add_many([e for e in evs if isinstance(e,dict)])
    upstream=os.getenv('SENDA_STUDIO_ARGUS_UPSTREAM','').rstrip('/')
    if upstream:
        headers={'Content-Type':'application/json'}
        key=os.getenv('SENDA_STUDIO_ARGUS_API_KEY','')
        if key: headers['X-API-Key']=key
        try:
            import urllib.request
            payload=json.dumps({'events':evs},ensure_ascii=False,default=str).encode()
            upstream_url=upstream+'/v1/agent-runs/ingest'
            logger.info('[senda-studio-argus] POST %s events=%d bytes=%d', upstream_url, len(evs), len(payload))
            def _forward():
                fwd=urllib.request.Request(upstream_url,data=payload,method='POST',headers=headers)
                with urllib.request.urlopen(fwd,timeout=5) as response:
                    return getattr(response,'status',None) or response.getcode()
            status=await asyncio.to_thread(_forward)
            logger.info('[senda-studio-argus] POST completed status=%s url=%s events=%d', status, upstream_url, len(evs))
        except Exception as exc:
            logger.warning('[senda-studio-argus] POST failed url=%s events=%d error=%s', upstream+'/v1/agent-runs/ingest', len(evs), exc)
    for e in evs:
        for q in list(subscribers):
            try: q.put_nowait(e)
            except asyncio.QueueFull: pass
    return {'accepted':accepted}

@app.get('/api/events/stream')
async def stream(request:Request, agent_id:str|None=None):
    q:asyncio.Queue=asyncio.Queue(maxsize=256); subscribers.add(q)
    async def gen():
        try:
            yield 'event: ready\ndata: {}\n\n'
            while True:
                if await request.is_disconnected(): break
                try: ev=await asyncio.wait_for(q.get(),15)
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'; continue
                if agent_id and ev.get('agent_id')!=agent_id: continue
                yield 'event: hook\ndata: '+json.dumps(ev,ensure_ascii=False,default=str)+'\n\n'
        finally: subscribers.discard(q)
    return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
