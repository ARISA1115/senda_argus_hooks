import json, os
inp=json.loads(os.getenv('SENDA_AGENT_INPUT','{}'))
target=inp.get('target') or inp.get('goal') or 'demo-target'
result={'target':target,'services':[{'port':443,'service':'https','banner':'nginx/1.24.0'},{'port':22,'service':'ssh','banner':'OpenSSH_9.6'}]}
print('[asset-discovery-agent] discovered services', flush=True)
print('[senda-agent-result] '+json.dumps(result,ensure_ascii=False), flush=True)
