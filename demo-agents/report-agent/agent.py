import json, os
inp=json.loads(os.getenv('SENDA_AGENT_INPUT','{}'))
result={'title':'Senda Argus Multi-Agent Demo Report','summary':'Workflow completed through registered Agent orchestration.','context':inp}
print('[report-agent] report generated', flush=True)
print('[senda-agent-result] '+json.dumps(result,ensure_ascii=False), flush=True)
