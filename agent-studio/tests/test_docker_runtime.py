import json
from app.docker_runtime import DockerRuntime, DockerAPIError, MANAGED_LABEL


class CaptureRuntime(DockerRuntime):
    def __init__(self):
        super().__init__('/tmp/fake.sock')
        self.calls=[]

    def _request(self, method, path, body=None, ok=(200,201,204), timeout=10):
        self.calls.append((method,path,body,ok,timeout))
        if method == 'POST' and path.startswith('/containers/create?'):
            return {'Id':'abc123456789xyz'}
        if method == 'GET' and path.endswith('/json'):
            return {'Config': {'Labels': {MANAGED_LABEL:'true'}}, 'State': {'Running': False}}
        return None


def test_create_mounts_host_agent_into_common_runtime():
    r=CaptureRuntime()
    out=r.create({
        'name':'soc-agent',
        'image':'senda/python-agent:0.8',
        'runtime':'python',
        'host_path':'/Users/demo/agents/soc-agent',
        'container_path':'/workspace',
        'entrypoint':'agent.py',
        'studio_endpoint':'http://senda-agent-studio:8080',
        'project':'default',
        'environment':'prod',
        'install_dependencies':True,
    })
    create=[c for c in r.calls if c[0]=='POST' and c[1].startswith('/containers/create?')][0]
    body=create[2]
    assert body['Image']=='senda/python-agent:0.8'
    assert body['WorkingDir']=='/workspace'
    assert body['HostConfig']['Binds']==['/Users/demo/agents/soc-agent:/workspace:rw']
    assert body['HostConfig']['RestartPolicy']=={'Name':'unless-stopped'}
    assert body['Cmd']==['python','agent.py']
    assert body['Labels'][MANAGED_LABEL]=='true'
    assert body['Labels']['com.senda.agent.host-path']=='/Users/demo/agents/soc-agent'
    assert 'SENDA_AGENT_INSTALL_DEPS=true' in body['Env']
    assert out['agent_id']=='soc-agent'


def test_reject_relative_host_path():
    r=CaptureRuntime()
    try:
        r.create({
            'name':'bad', 'image':'senda/python-agent:0.8', 'runtime':'python',
            'host_path':'relative/path', 'studio_endpoint':'http://studio:8080'
        })
    except DockerAPIError as e:
        assert 'absolute path' in str(e)
    else:
        raise AssertionError('expected DockerAPIError')


def test_stop_uses_short_grace_and_longer_socket_timeout():
    r=CaptureRuntime()
    r.stop('abc')
    call=r.calls[-1]
    assert call[1].endswith('/stop?t=3')
    assert call[-1]==15


def test_remove_only_managed_container_without_image_or_volume_delete():
    r=CaptureRuntime()
    r.remove('abc')
    delete=[c for c in r.calls if c[0]=='DELETE'][-1]
    assert delete[1].endswith('/containers/abc?v=0&force=0')
    assert delete[-1]==15


def test_remove_running_container_stops_then_deletes():
    class RunningRuntime(CaptureRuntime):
        def _request(self, method, path, body=None, ok=(200,201,204), timeout=10):
            self.calls.append((method,path,body,ok,timeout))
            if method=='GET' and path.endswith('/json'):
                return {'Config': {'Labels': {MANAGED_LABEL:'true'}}, 'State': {'Running': True}}
            return None
    r=RunningRuntime()
    r.remove('abc')
    methods=[c[0] for c in r.calls]
    assert methods==['GET','POST','DELETE']
    assert '/stop?t=3' in r.calls[1][1]

def test_decode_docker_multiplexed_logs():
    payload=b'hello\n'
    raw=bytes([1,0,0,0])+len(payload).to_bytes(4,'big')+payload
    assert DockerRuntime._decode_docker_log_stream(raw, False)=='hello\n'
    assert DockerRuntime._decode_docker_log_stream(b'plain\n', True)=='plain\n'


def test_create_supports_no_restart_policy():
    r=CaptureRuntime()
    r.create({
        'name':'batch-agent', 'image':'senda/python-agent:0.8', 'runtime':'python',
        'host_path':'/Users/demo/agents/batch-agent', 'studio_endpoint':'http://studio:8080',
        'restart_policy':'no',
    })
    create=[c for c in r.calls if c[0]=='POST' and c[1].startswith('/containers/create?')][0]
    body=create[2]
    assert body['HostConfig']['RestartPolicy']=={'Name':'no'}
    assert body['Labels']['com.senda.agent.restart-policy']=='no'


def test_create_supports_on_failure_retry_count():
    r=CaptureRuntime()
    r.create({
        'name':'retry-agent', 'image':'senda/python-agent:0.8', 'runtime':'python',
        'host_path':'/Users/demo/agents/retry-agent', 'studio_endpoint':'http://studio:8080',
        'restart_policy':'on-failure', 'maximum_retry_count':3,
    })
    create=[c for c in r.calls if c[0]=='POST' and c[1].startswith('/containers/create?')][0]
    body=create[2]
    assert body['HostConfig']['RestartPolicy']=={'Name':'on-failure','MaximumRetryCount':3}
    assert body['Labels']['com.senda.agent.maximum-retry-count']=='3'


def test_reject_unsupported_restart_policy():
    r=CaptureRuntime()
    try:
        r.create({
            'name':'bad-restart', 'image':'senda/python-agent:0.8', 'runtime':'python',
            'host_path':'/Users/demo/agents/bad-restart', 'studio_endpoint':'http://studio:8080',
            'restart_policy':'sometimes',
        })
    except DockerAPIError as e:
        assert 'Unsupported restart policy' in str(e)
    else:
        raise AssertionError('expected DockerAPIError')
