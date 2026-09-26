import asyncio

from app.orchestrator import WorkflowOrchestrator
from app.store import EventStore


class FakeRuntime:
    def __init__(self):
        self.started=[]
    def list_agents(self):
        return [
            {'agent_id':'asset-agent','name':'asset-agent','container_id':'c1','status':'exited','running':False,'image':'img','runtime':'python'},
            {'agent_id':'report-agent','name':'report-agent','container_id':'c2','status':'exited','running':False,'image':'img','runtime':'python'},
        ]
    def run_agent(self, agent_id, run_id, input_data=None, workflow_id=None, parent_agent_id=None, extra_env=None):
        self.started.append((agent_id,run_id,input_data,workflow_id,parent_agent_id))
        return {'run_id':run_id,'agent_id':agent_id}
    def wait_run(self, run_id, timeout=300):
        return {'run_id':run_id,'status':'exited','exit_code':0}
    def run_result(self, run_id):
        agent_id=next(x[0] for x in self.started if x[1]==run_id)
        return {'run_id':run_id,'result':{'from':agent_id,'ok':True},'logs':''}


def test_deterministic_multi_agent_workflow(tmp_path):
    store=EventStore(str(tmp_path/'orchestrator.db'))
    store.upsert_agent_profile('asset-agent', {'description':'asset discovery','capabilities':['asset-discovery']})
    store.upsert_agent_profile('report-agent', {'description':'final report','capabilities':['report']})
    wf=store.create_workflow(
        'asset discovery then report', {'target':'demo'}, 5,
        ['asset-agent','report-agent'], None, {'mode':'deterministic','agent_timeout':5},
    )
    rt=FakeRuntime()
    asyncio.run(WorkflowOrchestrator(store,rt).run(wf['workflow_id']))
    done=store.get_workflow(wf['workflow_id'])
    assert done['status']=='success'
    assert [s['agent_id'] for s in done['steps'] if s['action']=='run_agent']==['asset-agent','report-agent']
    assert len(rt.started)==2
    events=store.trace(run_id=wf['workflow_id'])
    assert any(e['event_type']=='orchestrator.plan.completed' for e in events)
    assert any(e['event_type']=='workflow.completed' for e in events)


def test_jev_supervisor_decision_telemetry(monkeypatch):
    from app.orchestrator import SupervisorPlanner

    planner = SupervisorPlanner({'mode': 'jev', 'jev_api_key': 'test-key', 'jev_model': 'jev-latest'})
    monkeypatch.setattr(
        planner,
        '_jev_system_one',
        lambda state, criteria: {
            'choice': 'report-agent',
            'confidence': 0.91,
            'probabilities': {'asset-agent': 0.02, 'report-agent': 0.91, '__finish__': 0.07},
            'model': 'jev-test',
            'request_id': 'req-test',
            'latency_ms': 12.5,
            'usage': {'input_tokens': 42, 'output_tokens': 0},
        },
    )
    decision = planner.plan(
        'produce a report',
        {'target': 'demo'},
        [
            {'agent_id': 'asset-agent', 'description': 'discover assets', 'capabilities': ['asset-discovery']},
            {'agent_id': 'report-agent', 'description': 'produce final report', 'capabilities': ['report']},
        ],
        [{'action': 'run_agent', 'agent_id': 'asset-agent', 'output': {'services': [443]}}],
    )
    assert decision['action'] == 'run_agent'
    assert decision['agent_id'] == 'report-agent'
    assert decision['_supervisor']['backend'] == 'jev'
    assert decision['_supervisor']['confidence'] == 0.91
    assert decision['_supervisor']['probabilities']['report-agent'] == 0.91


def test_jev_supervisor_finish(monkeypatch):
    from app.orchestrator import SupervisorPlanner

    planner = SupervisorPlanner({'mode': 'jev', 'jev_api_key': 'test-key'})
    monkeypatch.setattr(
        planner,
        '_jev_system_one',
        lambda state, criteria: {
            'choice': '__finish__',
            'confidence': 0.88,
            'probabilities': {'report-agent': 0.12, '__finish__': 0.88},
            'model': 'jev-test',
            'request_id': 'req-finish',
            'latency_ms': 9.0,
            'usage': {},
        },
    )
    decision = planner.plan(
        'produce a report',
        {},
        [{'agent_id': 'report-agent', 'description': 'produce final report'}],
        [{'action': 'run_agent', 'agent_id': 'report-agent', 'output': {'report': 'done'}}],
    )
    assert decision['action'] == 'finish'
    assert decision['final_result'] == {'report': 'done'}
    assert decision['_supervisor']['selected'] == '__finish__'
