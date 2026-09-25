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
