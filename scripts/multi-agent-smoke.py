#!/usr/bin/env python3
"""Register deterministic demo Agents and execute an offline multi-Agent workflow."""
from __future__ import annotations
import json, os, sys, time, urllib.error, urllib.request
from pathlib import Path

BASE=os.getenv('SENDA_STUDIO_URL','http://localhost:8080').rstrip('/')
ROOT=Path(__file__).resolve().parent.parent
IMAGE=os.getenv('SENDA_RUNTIME_IMAGE','senda/python-agent:0.8')


def req(method,path,body=None):
    data=None if body is None else json.dumps(body).encode()
    r=urllib.request.Request(BASE+path,data=data,method=method,headers={'Content-Type':'application/json'} if data else {})
    try:
        with urllib.request.urlopen(r,timeout=30) as resp:
            raw=resp.read()
            return json.loads(raw.decode()) if raw else {'ok':True}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'{method} {path}: HTTP {e.code}: {e.read().decode(errors="replace")}') from e

agents=[
    ('asset-discovery-agent','Discover target services',['asset-discovery','service-discovery']),
    ('vulnerability-agent','Analyze service findings for vulnerabilities',['vulnerability-analysis','cve-analysis']),
    ('report-agent','Produce a final report',['report','summarization']),
]

existing={a['agent_id'] for a in req('GET','/api/agents')['agents']}
for agent_id,description,capabilities in agents:
    if agent_id in existing:
        print(f'[smoke] already registered: {agent_id}')
        continue
    body={
        'name':agent_id,
        'image':IMAGE,
        'runtime':'python',
        'host_path':str((ROOT/'demo-agents'/agent_id).resolve()),
        'container_path':'/workspace',
        'entrypoint':'agent.py',
        'install_dependencies':True,
        'restart_policy':'no',
        'description':description,
        'capabilities':capabilities,
        'tags':['demo','multi-agent'],
        'risk_level':'low',
    }
    out=req('POST','/api/agents',body)
    print(f'[smoke] registered: {out["agent_id"]}')

wf=req('POST','/api/workflows',{
    'goal':'Discover assets, analyze vulnerabilities, and produce a final report.',
    'input':{'target':'demo-target'},
    'allowed_agents':[a[0] for a in agents],
    'max_steps':6,
    'llm':{'mode':'deterministic','agent_timeout':120},
})
wid=wf['workflow_id']
print(f'[smoke] workflow: {wid}')
for _ in range(120):
    wf=req('GET',f'/api/workflows/{wid}')
    print(f'[smoke] status={wf["status"]} steps={len(wf.get("steps") or [])}')
    if wf['status'] in {'success','failed','cancelled','pending_approval'}:
        print(json.dumps(wf,ensure_ascii=False,indent=2))
        sys.exit(0 if wf['status']=='success' else 1)
    time.sleep(1)
raise SystemExit('workflow timed out')
