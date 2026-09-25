import os
os.environ['SENDA_STUDIO_DB']='/tmp/senda-studio-test.db'
try:
    os.unlink('/tmp/senda-studio-test.db')
except FileNotFoundError:
    pass
from fastapi.testclient import TestClient
import app.main as m

class FakeRuntime:
    def __init__(self): self.actions=[]
    def list_agents(self): return [{'name':'a','container_id':'cid','id':'cid','status':'running','running':True,'image':'img','runtime':'python','agent_id':'a','project':'p','environment':'prod','started_at':'','finished_at':'','restart_count':0}]
    def start(self,i): self.actions.append(('start',i))
    def stop(self,i): self.actions.append(('stop',i))
    def restart(self,i): self.actions.append(('restart',i))
    def create(self,s): return {'name':s['name'],'container_id':'new','id':'new','agent_id':s.get('agent_id') or s['name']}

fake=FakeRuntime(); m._runtime=fake
c=TestClient(m.app)

def test_agents_and_actions():
    assert c.get('/api/agents').json()['agents'][0]['name']=='a'
    assert c.post('/api/agents/cid/stop').status_code==200
    assert fake.actions[-1]==('stop','cid')

def test_create_and_ingest():
    r=c.post('/api/agents',json={'name':'new-agent','image':'img'})
    assert r.status_code==200
    e={'event_id':'api-e1','timestamp':'2026-01-01T00:00:00Z','agent_id':'new-agent','trace_id':'t','event_type':'mcp.tool_call.requested'}
    assert c.post('/v1/agent-runs/ingest',json={'events':[e]}).status_code==200
    assert c.get('/api/traces?agent_id=new-agent').json()['events'][0]['event_id']=='api-e1'
