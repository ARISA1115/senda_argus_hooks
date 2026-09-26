import os
os.environ['SENDA_STUDIO_DB']='/tmp/senda-studio-test.db'
try:
    os.unlink('/tmp/senda-studio-test.db')
except FileNotFoundError:
    pass
from fastapi.testclient import TestClient
import app.main as m

class FakeRuntime:
    def __init__(self):
        self.actions=[]
        self.created={}
        self.created_specs=[]
    def list_agents(self):
        base=[{'name':'a','container_id':'cid','id':'cid','status':'running','running':True,'image':'img','runtime':'python','agent_id':'a','project':'p','environment':'prod','started_at':'','finished_at':'','restart_count':0}]
        return base+list(self.created.values())
    def start(self,i): self.actions.append(('start',i))
    def stop(self,i): self.actions.append(('stop',i))
    def restart(self,i): self.actions.append(('restart',i))
    def remove(self,i): self.actions.append(('delete',i))
    def create(self,s):
        self.created_specs.append(dict(s))
        aid=s.get('agent_id') or s['name']
        row={'name':s['name'],'container_id':'new-'+aid,'id':'new-'+aid,'status':'exited','running':False,'image':s['image'],'runtime':s.get('runtime','python'),'agent_id':aid,'project':s.get('project','default'),'environment':s.get('environment','prod')}
        self.created[aid]=row
        return row
    def run_agent(self,agent_id,run_id,input_data=None,workflow_id=None,parent_agent_id=None,extra_env=None): return {'agent_id':agent_id,'run_id':run_id,'status':'running'}
    def list_runs(self,workflow_id=None,limit=100): return []
    def get_run(self,run_id): return {'run_id':run_id,'status':'exited','exit_code':0}
    def wait_run(self,run_id,timeout=300): return self.get_run(run_id)
    def run_result(self,run_id): return {'run_id':run_id,'result':{'ok':True},'logs':''}
    def remove_run(self,run_id): self.actions.append(('delete-run',run_id))
    def stop_run(self,run_id): self.actions.append(('stop-run',run_id))

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


def test_agent_registry_metadata_and_run_policy():
    r=c.post('/api/agents',json={
      'name':'meta-agent','image':'img','description':'CVE analyzer',
      'capabilities':['cve-analysis'],'risk_level':'high','requires_approval':True
    })
    assert r.status_code==200
    a=c.get('/api/agents/meta-agent').json()
    assert a['description']=='CVE analyzer'
    assert a['capabilities']==['cve-analysis']
    denied=c.post('/api/agents/meta-agent/run',json={'input':{'x':1}})
    assert denied.status_code==409


def test_direct_one_shot_run_for_unrestricted_agent():
    c.post('/api/agents',json={'name':'worker-agent','image':'img','restart_policy':'no'})
    r=c.post('/api/agents/worker-agent/run',json={'input':{'target':'demo'},'caller_id':'mcp-external'})
    assert r.status_code==200
    assert r.json()['agent_id']=='worker-agent'


def test_webui_i18n_assets():
    index=c.get('/')
    assert index.status_code==200
    assert 'id="languageSelect"' in index.text
    assert 'data-i18n="workflow.list_title"' in index.text
    assert 'id="navWorkflowCreate"' in index.text
    assert 'id="workflowCreateView"' in index.text
    assert 'id="workflowView"' in index.text
    catalog=c.get('/static/i18n.js')
    assert catalog.status_code==200
    assert "'ja':" not in catalog.text  # catalog uses object keys without quoted top-level labels
    assert "ja:" in catalog.text and "en:" in catalog.text
    assert 'senda-agent-studio.language' in catalog.text


def test_agent_register_only_flag_reaches_runtime():
    r=c.post('/api/agents',json={'name':'registered-only-agent','image':'img','start_immediately':False})
    assert r.status_code==200
    assert fake.created_specs[-1]['start_immediately'] is False


def test_workflow_can_register_without_run_and_delete():
    if not any(a.get('agent_id')=='worker-agent' for a in fake.list_agents()):
        c.post('/api/agents',json={'name':'worker-agent','image':'img','restart_policy':'no','start_immediately':False})
    r=c.post('/api/workflows',json={
        'goal':'registered workflow test',
        'allowed_agents':['worker-agent'],
        'llm':{'mode':'deterministic'},
        'start_immediately':False,
    })
    assert r.status_code==200
    wf=r.json()
    assert wf['status']=='registered'
    workflow_id=wf['workflow_id']
    d=c.delete('/api/workflows/'+workflow_id)
    assert d.status_code==200
    assert c.get('/api/workflows/'+workflow_id).status_code==404
