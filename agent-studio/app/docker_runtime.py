from __future__ import annotations
import http.client, json, socket
from typing import Any
from urllib.parse import quote, urlencode

SOCKET_PATH='/var/run/docker.sock'
MANAGED_LABEL='com.senda.agent.managed'

class DockerAPIError(RuntimeError): pass

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path=SOCKET_PATH, timeout=10):
        super().__init__('localhost', timeout=timeout); self.path=path
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout); self.sock.connect(self.path)

class DockerRuntime:
    def __init__(self, socket_path:str=SOCKET_PATH): self.socket_path=socket_path
    def _request(self, method:str, path:str, body:dict[str,Any]|None=None, ok=(200,201,204)):
        c=UnixHTTPConnection(self.socket_path)
        data=None if body is None else json.dumps(body).encode()
        headers={'Content-Type':'application/json'} if data is not None else {}
        try:
            c.request(method,path,body=data,headers=headers); r=c.getresponse(); raw=r.read()
        except OSError as e: raise DockerAPIError(f'Docker socket unavailable: {e}') from e
        finally: c.close()
        parsed=None
        if raw:
            try: parsed=json.loads(raw)
            except Exception: parsed=raw.decode(errors='replace')
        if r.status not in ok:
            msg=parsed.get('message') if isinstance(parsed,dict) else parsed
            raise DockerAPIError(f'Docker API {r.status}: {msg or r.reason}')
        return parsed

    def list_agents(self):
        filters=json.dumps({'label':[f'{MANAGED_LABEL}=true']})
        rows=self._request('GET','/containers/json?'+urlencode({'all':'1','filters':filters})) or []
        out=[]
        for c in rows:
            labels=c.get('Labels') or {}; state=c.get('State') or 'unknown'
            out.append({
                'id':str(c.get('Id',''))[:12], 'container_id':c.get('Id',''),
                'name':(c.get('Names') or ['/unknown'])[0].lstrip('/'), 'status':state,
                'running':state=='running', 'image':c.get('Image',''),
                'runtime':labels.get('com.senda.agent.runtime','unknown'),
                'agent_id':labels.get('com.senda.agent.id') or (c.get('Names') or ['/unknown'])[0].lstrip('/'),
                'project':labels.get('com.senda.agent.project','default'),
                'environment':labels.get('com.senda.agent.environment','prod'),
                'started_at':None, 'finished_at':None, 'restart_count':None,
            })
        return sorted(out,key=lambda x:x['name'])

    def start(self, ident): self._request('POST',f'/containers/{quote(ident,safe="")}/start',ok=(204,304))
    def stop(self, ident): self._request('POST',f'/containers/{quote(ident,safe="")}/stop?t=10',ok=(204,304))
    def restart(self, ident): self._request('POST',f'/containers/{quote(ident,safe="")}/restart?t=10',ok=(204,))

    def create(self, spec:dict[str,Any]):
        name=spec['name']; agent_id=spec.get('agent_id') or name; network=spec.get('network','senda-agent-net')
        env={
            'SENDA_ARGUS_ENABLED':'true','SENDA_ARGUS_EXPORTER':'argus','SENDA_ARGUS_EXPORTERS':'argus',
            'SENDA_ARGUS_ENDPOINT':spec['studio_endpoint'].rstrip('/'),'SENDA_ARGUS_AGENT_ID':agent_id,
            'SENDA_ARGUS_PROJECT':spec.get('project','default'),'SENDA_ARGUS_ENVIRONMENT':spec.get('environment','prod'),
        }; env.update(spec.get('env') or {})
        labels={MANAGED_LABEL:'true','com.senda.agent.id':agent_id,'com.senda.agent.runtime':spec.get('runtime','python'),
                'com.senda.agent.project':spec.get('project','default'),'com.senda.agent.environment':spec.get('environment','prod')}
        body={'Image':spec['image'],'Env':[f'{k}={v}' for k,v in env.items()],'Labels':labels,
              'HostConfig':{'RestartPolicy':{'Name':spec.get('restart_policy','unless-stopped')},'NetworkMode':network}}
        if spec.get('command'):
            import shlex; body['Cmd']=shlex.split(spec['command'])
        created=self._request('POST','/containers/create?'+urlencode({'name':name}),body,ok=(201,)) or {}
        cid=created.get('Id',''); self.start(cid)
        return {'id':cid[:12],'container_id':cid,'name':name,'agent_id':agent_id}
