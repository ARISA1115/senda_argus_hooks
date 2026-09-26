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
    def remove(self,i): self.actions.append(('delete',i))
    def create(self,s): return {'name':s['name'],'container_id':'new','id':'new','agent_id':s.get('agent_id') or s['name']}

fake=FakeRuntime(); m._runtime=fake
c=TestClient(m.app)

def test_agents_and_actions():
    assert c.get('/api/agents').json()['agents'][0]['name']=='a'
    assert c.post('/api/agents/cid/stop').status_code==200
    assert fake.actions[-1]==('stop','cid')
    assert c.delete('/api/agents/cid').status_code==200
    assert fake.actions[-1]==('delete','cid')

def test_create_and_ingest():
    r=c.post('/api/agents',json={'name':'new-agent','image':'img'})
    assert r.status_code==200
    e={'event_id':'api-e1','timestamp':'2026-01-01T00:00:00Z','agent_id':'new-agent','trace_id':'t','event_type':'mcp.tool_call.requested'}
    assert c.post('/v1/agent-runs/ingest',json={'events':[e]}).status_code==200
    assert c.get('/api/traces?agent_id=new-agent').json()['events'][0]['event_id']=='api-e1'

def test_trace_detail_api_and_logs_api():
    fake.logs=lambda ident,tail=200: 'line1\nline2\n'
    r=c.get('/api/agents/cid/logs?tail=50')
    assert r.status_code==200 and 'line1' in r.json()['logs']
    e1={'event_id':'api-d1','timestamp':'2026-01-01T00:00:00Z','agent_id':'new-agent','trace_id':'td','run_id':'rd','event_type':'llm.request','model':'m','prompt':'p'}
    e2={'event_id':'api-d2','timestamp':'2026-01-01T00:00:01Z','agent_id':'new-agent','trace_id':'td','run_id':'rd','event_type':'llm.response','response':'r'}
    c.post('/v1/agent-runs/ingest',json={'events':[e1,e2]})
    d=c.get('/api/traces/detail?trace_id=td').json()
    assert d['summary']['model']=='m' and d['summary']['response']=='r'


def test_restart_policy_validation():
    assert c.post('/api/agents',json={'name':'valid-no','image':'img','restart_policy':'no'}).status_code==200
    assert c.post('/api/agents',json={'name':'valid-retry','image':'img','restart_policy':'on-failure','maximum_retry_count':3}).status_code==200
    assert c.post('/api/agents',json={'name':'bad-policy','image':'img','restart_policy':'sometimes'}).status_code==422
