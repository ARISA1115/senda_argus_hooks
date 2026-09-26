from app.store import EventStore

def test_store_trace(tmp_path):
    s=EventStore(str(tmp_path/'x.db'))
    e={'event_id':'e1','timestamp':'2026-01-01T00:00:00Z','agent_id':'a1','run_id':'r1','trace_id':'t1','event_type':'llm.request','status':'ok'}
    assert s.add_many([e])==1
    assert s.add_many([e])==0
    assert s.recent('a1')[0]['event_id']=='e1'
    assert s.trace(trace_id='t1')[0]['run_id']=='r1'

def test_trace_groups(tmp_path):
    s=EventStore(str(tmp_path/'g.db'))
    s.add_many([
      {'event_id':'g1','timestamp':'2026-01-01T00:00:00Z','agent_id':'a','run_id':'r','trace_id':'t','event_type':'llm.request'},
      {'event_id':'g2','timestamp':'2026-01-01T00:00:01Z','agent_id':'a','run_id':'r','trace_id':'t','event_type':'llm.response'},
    ])
    g=s.trace_groups('a')[0]
    assert g['trace_id']=='t' and g['event_count']==2

def test_trace_detail_extracts_model_prompt_response_and_latency(tmp_path):
    s=EventStore(str(tmp_path/'detail.db'))
    s.add_many([
      {'event_id':'d1','timestamp':'2026-01-01T00:00:00Z','agent_id':'a','run_id':'r','trace_id':'t','event_type':'llm.request','status':'success','model':'llama3.1','prompt':'hello'},
      {'event_id':'d2','timestamp':'2026-01-01T00:00:01Z','agent_id':'a','run_id':'r','trace_id':'t','event_type':'llm.response','status':'success','response':'world'},
    ])
    d=s.trace_detail(trace_id='t')
    assert d['summary']['model']=='llama3.1'
    assert d['summary']['prompt']=='hello'
    assert d['summary']['response']=='world'
    assert d['summary']['latency_ms']==1000.0
    assert d['summary']['event_count']==2


def test_agent_profile_and_workflow_state(tmp_path):
    s=EventStore(str(tmp_path/'multi.db'))
    s.upsert_agent_profile('vuln-agent', {
        'description':'Analyze vulnerabilities',
        'capabilities':['cve-analysis'],
        'tags':['security'],
        'input_schema':{'type':'object'},
        'output_schema':{'type':'object'},
        'risk_level':'medium',
        'requires_approval':True,
        'allowed_callers':['senda-orchestrator'],
    })
    p=s.get_agent_profile('vuln-agent')
    assert p['capabilities']==['cve-analysis']
    assert p['requires_approval'] is True
    wf=s.create_workflow('analyze target', {'target':'demo'}, 4, ['vuln-agent'], 'vuln-agent', {'mode':'deterministic'})
    assert wf['status']=='queued'
    s.add_workflow_step(wf['workflow_id'],0,'run_agent','running',agent_id='vuln-agent',input_data={'target':'demo'})
    s.update_workflow_step(wf['workflow_id'],0,status='success',output={'ok':True})
    d=s.get_workflow(wf['workflow_id'])
    assert d['steps'][0]['output']=={'ok':True}


def test_workflow_reset_and_delete(tmp_path):
    s=EventStore(str(tmp_path/'workflow-lifecycle.db'))
    wf=s.create_workflow('demo', {}, 3, [], None, {'mode':'deterministic'}, status='registered')
    assert wf['status']=='registered'
    s.add_workflow_step(wf['workflow_id'],0,'run_agent','success',agent_id='demo-agent',output_data={'ok':True})
    s.update_workflow(wf['workflow_id'],status='success',result={'done':True})
    reset=s.reset_workflow(wf['workflow_id'])
    assert reset['status']=='queued'
    assert reset['steps']==[] and reset['result'] is None
    s.delete_workflow(wf['workflow_id'])
    try:
        s.get_workflow(wf['workflow_id'])
    except KeyError:
        pass
    else:
        raise AssertionError('workflow should be deleted')
