from __future__ import annotations

import http.client
import json
import shlex
import socket
from typing import Any
from urllib.parse import quote, urlencode

SOCKET_PATH = '/var/run/docker.sock'
MANAGED_LABEL = 'com.senda.agent-runtime'
LEGACY_MANAGED_LABEL = 'com.senda.agent.managed'
RESTART_POLICIES = {'no', 'on-failure', 'always', 'unless-stopped'}
RUNTIME_KIND_LABEL = 'com.senda.agent.runtime-kind'
RUN_ID_LABEL = 'com.senda.agent.run-id'
WORKFLOW_ID_LABEL = 'com.senda.agent.workflow-id'
PARENT_AGENT_LABEL = 'com.senda.agent.parent-agent-id'


class DockerAPIError(RuntimeError):
    pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str = SOCKET_PATH, timeout: float = 10):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DockerRuntime:
    def __init__(self, socket_path: str = SOCKET_PATH):
        self.socket_path = socket_path

    def _request_raw(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
        timeout: float = 10,
    ) -> tuple[int, bytes, str]:
        c = UnixHTTPConnection(self.socket_path, timeout=timeout)
        data = None if body is None else json.dumps(body).encode()
        headers = {'Content-Type': 'application/json'} if data is not None else {}
        try:
            c.request(method, path, body=data, headers=headers)
            r = c.getresponse()
            raw = r.read()
            status, reason = r.status, r.reason
        except socket.timeout as e:
            raise DockerAPIError(f'Docker API request timed out after {timeout:g}s') from e
        except OSError as e:
            raise DockerAPIError(f'Docker socket unavailable: {e}') from e
        finally:
            c.close()

        if status not in ok:
            parsed: Any = None
            if raw:
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = raw.decode(errors='replace')
            msg = parsed.get('message') if isinstance(parsed, dict) else parsed
            raise DockerAPIError(f'Docker API {status}: {msg or reason}')
        return status, raw, reason

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
        timeout: float = 10,
    ):
        _, raw, _ = self._request_raw(method, path, body=body, ok=ok, timeout=timeout)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw.decode(errors='replace')

    @staticmethod
    def _is_managed(labels: dict[str, str] | None) -> bool:
        labels = labels or {}
        return labels.get(MANAGED_LABEL) == 'true' or labels.get(LEGACY_MANAGED_LABEL) == 'true'

    def _inspect_managed(self, ident: str) -> dict[str, Any]:
        info = self._request('GET', f'/containers/{quote(ident, safe="")}/json') or {}
        labels = ((info.get('Config') or {}).get('Labels') or {})
        if not self._is_managed(labels):
            raise DockerAPIError('Container is not a Senda Agent Runtime')
        return info

    def list_agents(self):
        filters = json.dumps({'label': [f'{MANAGED_LABEL}=true']})
        rows = self._request('GET', '/containers/json?' + urlencode({'all': '1', 'filters': filters})) or []
        legacy_filters = json.dumps({'label': [f'{LEGACY_MANAGED_LABEL}=true']})
        legacy_rows = self._request('GET', '/containers/json?' + urlencode({'all': '1', 'filters': legacy_filters})) or []

        by_id: dict[str, dict[str, Any]] = {}
        for c in [*rows, *legacy_rows]:
            by_id[c.get('Id', '')] = c

        out = []
        for c in by_id.values():
            labels = c.get('Labels') or {}
            if not self._is_managed(labels):
                continue
            if labels.get(RUNTIME_KIND_LABEL) == 'run':
                continue
            state = c.get('State') or 'unknown'
            mounts = c.get('Mounts') or []
            source_mount = next((m for m in mounts if m.get('Destination') == labels.get('com.senda.agent.container-path', '/workspace')), None)
            restart_policy = labels.get('com.senda.agent.restart-policy', 'unknown')
            maximum_retry_count = int(labels.get('com.senda.agent.maximum-retry-count', '0') or 0)
            try:
                inspected = self._request('GET', f'/containers/{quote(c.get("Id", ""), safe="")}/json') or {}
                rp = ((inspected.get('HostConfig') or {}).get('RestartPolicy') or {})
                restart_policy = rp.get('Name') or restart_policy
                maximum_retry_count = int(rp.get('MaximumRetryCount') or 0)
            except DockerAPIError:
                pass
            out.append({
                'id': str(c.get('Id', ''))[:12],
                'container_id': c.get('Id', ''),
                'name': (c.get('Names') or ['/unknown'])[0].lstrip('/'),
                'status': state,
                'running': state == 'running',
                'image': c.get('Image', ''),
                'runtime': labels.get('com.senda.agent.runtime', 'unknown'),
                'agent_id': labels.get('com.senda.agent.id') or (c.get('Names') or ['/unknown'])[0].lstrip('/'),
                'project': labels.get('com.senda.agent.project', 'default'),
                'environment': labels.get('com.senda.agent.environment', 'prod'),
                'host_path': labels.get('com.senda.agent.host-path') or (source_mount or {}).get('Source'),
                'container_path': labels.get('com.senda.agent.container-path', '/workspace'),
                'entrypoint': labels.get('com.senda.agent.entrypoint', ''),
                'restart_policy': restart_policy,
                'maximum_retry_count': maximum_retry_count,
            })
        return sorted(out, key=lambda x: x['name'])

    def start(self, ident: str):
        self._inspect_managed(ident)
        self._request('POST', f'/containers/{quote(ident, safe="")}/start', ok=(204, 304))

    def stop(self, ident: str):
        self._inspect_managed(ident)
        self._request('POST', f'/containers/{quote(ident, safe="")}/stop?t=3', ok=(204, 304), timeout=15)

    def restart(self, ident: str):
        self._inspect_managed(ident)
        self._request('POST', f'/containers/{quote(ident, safe="")}/restart?t=3', ok=(204,), timeout=20)

    def remove(self, ident: str):
        info = self._inspect_managed(ident)
        state = info.get('State') or {}
        if state.get('Running'):
            self._request('POST', f'/containers/{quote(ident, safe="")}/stop?t=3', ok=(204, 304), timeout=15)
        self._request('DELETE', f'/containers/{quote(ident, safe="")}?v=0&force=0', ok=(204,), timeout=15)

    @staticmethod
    def _decode_docker_log_stream(raw: bytes, tty: bool) -> str:
        if tty or not raw:
            return raw.decode(errors='replace')
        # Docker multiplexed stdout/stderr format: 8-byte header, then payload.
        pos = 0
        chunks: list[bytes] = []
        while pos + 8 <= len(raw):
            header = raw[pos:pos + 8]
            stream_type = header[0]
            size = int.from_bytes(header[4:8], 'big')
            end = pos + 8 + size
            if stream_type not in (0, 1, 2, 3) or size < 0 or end > len(raw):
                return raw.decode(errors='replace')
            chunks.append(raw[pos + 8:end])
            pos = end
        if pos != len(raw):
            return raw.decode(errors='replace')
        return b''.join(chunks).decode(errors='replace')

    def logs(self, ident: str, tail: int = 200) -> str:
        info = self._inspect_managed(ident)
        tail = max(1, min(int(tail), 2000))
        query = urlencode({'stdout': '1', 'stderr': '1', 'timestamps': '1', 'tail': str(tail)})
        _, raw, _ = self._request_raw(
            'GET',
            f'/containers/{quote(ident, safe="")}/logs?{query}',
            ok=(200,),
            timeout=15,
        )
        tty = bool((info.get('Config') or {}).get('Tty'))
        return self._decode_docker_log_stream(raw, tty)

    def create(self, spec: dict[str, Any]):
        name = spec['name']
        agent_id = spec.get('agent_id') or name
        network = spec.get('network', 'senda-agent-net')
        runtime = spec.get('runtime', 'python')
        host_path = (spec.get('host_path') or '').strip()
        container_path = (spec.get('container_path') or '/workspace').strip()
        entrypoint = (spec.get('entrypoint') or '').strip()
        restart_policy = (spec.get('restart_policy') or 'unless-stopped').strip()
        maximum_retry_count = int(spec.get('maximum_retry_count') or 0)

        if restart_policy not in RESTART_POLICIES:
            raise DockerAPIError(f'Unsupported restart policy: {restart_policy}')
        if maximum_retry_count < 0:
            raise DockerAPIError('Maximum Retry Count must be 0 or greater')
        if runtime == 'python' and not entrypoint:
            entrypoint = 'agent.py'
        if host_path and not host_path.startswith('/'):
            raise DockerAPIError('Host Agent Path must be an absolute path')
        if not container_path.startswith('/'):
            raise DockerAPIError('Container Path must be an absolute path')
        if '..' in container_path.split('/'):
            raise DockerAPIError('Container Path must not contain ..')

        env = {
            'SENDA_ARGUS_ENABLED': 'true',
            'SENDA_ARGUS_EXPORTER': 'argus',
            'SENDA_ARGUS_EXPORTERS': 'argus',
            'SENDA_ARGUS_ENDPOINT': spec['studio_endpoint'].rstrip('/'),
            'SENDA_ARGUS_AGENT_ID': agent_id,
            'SENDA_ARGUS_PROJECT': spec.get('project', 'default'),
            'SENDA_ARGUS_ENVIRONMENT': spec.get('environment', 'prod'),
            'SENDA_ARGUS_HTTP_LOG': 'true',
            'SENDA_AGENT_INSTALL_DEPS': 'true' if spec.get('install_dependencies', True) else 'false',
            'SENDA_AGENT_WORKSPACE': container_path,
        }
        env.update(spec.get('env') or {})

        labels = {
            MANAGED_LABEL: 'true',
            'com.senda.agent.id': agent_id,
            'com.senda.agent.runtime': runtime,
            'com.senda.agent.project': spec.get('project', 'default'),
            'com.senda.agent.environment': spec.get('environment', 'prod'),
            'com.senda.agent.host-path': host_path,
            'com.senda.agent.container-path': container_path,
            'com.senda.agent.entrypoint': entrypoint,
            'com.senda.agent.restart-policy': restart_policy,
            'com.senda.agent.maximum-retry-count': str(maximum_retry_count if restart_policy == 'on-failure' else 0),
            RUNTIME_KIND_LABEL: 'template',
        }

        docker_restart_policy: dict[str, Any] = {'Name': restart_policy}
        if restart_policy == 'on-failure':
            docker_restart_policy['MaximumRetryCount'] = maximum_retry_count

        host_config: dict[str, Any] = {
            'RestartPolicy': docker_restart_policy,
            'NetworkMode': network,
        }
        if host_path:
            host_config['Binds'] = [f'{host_path}:{container_path}:rw']

        body: dict[str, Any] = {
            'Image': spec['image'],
            'Env': [f'{k}={v}' for k, v in env.items()],
            'Labels': labels,
            'HostConfig': host_config,
            'WorkingDir': container_path if host_path else '/app',
        }

        command = (spec.get('command') or '').strip()
        if command:
            body['Cmd'] = shlex.split(command)
        elif host_path:
            if runtime == 'python':
                body['Cmd'] = ['python', entrypoint]
            elif runtime == 'node':
                body['Cmd'] = ['node', entrypoint or 'index.js']

        created = self._request('POST', '/containers/create?' + urlencode({'name': name}), body, ok=(201,)) or {}
        cid = created.get('Id', '')
        if spec.get('start_immediately', True):
            self.start(cid)
        return {'id': cid[:12], 'container_id': cid, 'name': name, 'agent_id': agent_id, 'started': bool(spec.get('start_immediately', True))}


    def _find_registered_agent(self, agent_id: str) -> dict[str, Any]:
        for agent in self.list_agents():
            if agent.get('agent_id') == agent_id or agent.get('name') == agent_id or agent.get('container_id') == agent_id:
                return agent
        raise KeyError(agent_id)

    @staticmethod
    def _env_list_to_dict(items: list[str] | None) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in items or []:
            if '=' in item:
                k, v = item.split('=', 1)
                out[k] = v
        return out

    def run_agent(
        self,
        agent_id: str,
        run_id: str,
        input_data: Any = None,
        workflow_id: str | None = None,
        parent_agent_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run a registered Agent as a one-shot child container.

        The registered Runtime is treated as a template.  A child container is
        created with the same image, bind mounts, command and network, but with
        restart=no and run/workflow identity injected into the environment.
        """
        template = self._find_registered_agent(agent_id)
        info = self._inspect_managed(template['container_id'])
        cfg = info.get('Config') or {}
        host_cfg = info.get('HostConfig') or {}
        labels = dict(cfg.get('Labels') or {})
        labels[MANAGED_LABEL] = 'true'
        labels[RUNTIME_KIND_LABEL] = 'run'
        labels[RUN_ID_LABEL] = run_id
        labels['com.senda.agent.template-id'] = str(template.get('agent_id') or agent_id)
        if workflow_id:
            labels[WORKFLOW_ID_LABEL] = workflow_id
        if parent_agent_id:
            labels[PARENT_AGENT_LABEL] = parent_agent_id

        env = self._env_list_to_dict(cfg.get('Env'))
        env['SENDA_ARGUS_AGENT_ID'] = str(template.get('agent_id') or agent_id)
        env['SENDA_ARGUS_RUN_ID'] = run_id
        env['SENDA_AGENT_RUN_ID'] = run_id
        env['SENDA_AGENT_INPUT'] = json.dumps(input_data if input_data is not None else {}, ensure_ascii=False, default=str)
        if workflow_id:
            env['SENDA_WORKFLOW_RUN_ID'] = workflow_id
        if parent_agent_id:
            env['SENDA_PARENT_AGENT_ID'] = parent_agent_id
        env.update(extra_env or {})

        run_host_config: dict[str, Any] = {
            'RestartPolicy': {'Name': 'no'},
            'NetworkMode': host_cfg.get('NetworkMode') or 'senda-agent-net',
        }
        # Preserve the settings relevant to a normal mounted Agent runtime.
        for key in ('Binds', 'Mounts', 'ReadonlyRootfs', 'Dns', 'ExtraHosts', 'SecurityOpt'):
            if host_cfg.get(key):
                run_host_config[key] = host_cfg[key]

        body: dict[str, Any] = {
            'Image': cfg.get('Image') or template.get('image'),
            'Env': [f'{k}={v}' for k, v in env.items()],
            'Labels': labels,
            'HostConfig': run_host_config,
            'WorkingDir': cfg.get('WorkingDir') or template.get('container_path') or '/workspace',
            'Cmd': cfg.get('Cmd'),
        }
        if cfg.get('Entrypoint'):
            body['Entrypoint'] = cfg.get('Entrypoint')
        # Docker rejects explicit null Cmd in a few versions.
        if body.get('Cmd') is None:
            body.pop('Cmd', None)

        safe_agent = ''.join(ch if ch.isalnum() or ch in '_.-' else '-' for ch in str(template.get('agent_id') or agent_id))[:40]
        safe_run = ''.join(ch if ch.isalnum() or ch in '_.-' else '-' for ch in run_id)[-20:]
        name = f'{safe_agent}-run-{safe_run}'[:63]
        created = self._request('POST', '/containers/create?' + urlencode({'name': name}), body, ok=(201,)) or {}
        cid = created.get('Id', '')
        self.start(cid)
        return {
            'run_id': run_id,
            'container_id': cid,
            'id': cid[:12],
            'name': name,
            'agent_id': template.get('agent_id') or agent_id,
            'workflow_id': workflow_id,
            'status': 'running',
        }

    def _find_run_container(self, run_id: str) -> dict[str, Any]:
        filters = json.dumps({'label': [f'{RUN_ID_LABEL}={run_id}', f'{RUNTIME_KIND_LABEL}=run']})
        rows = self._request('GET', '/containers/json?' + urlencode({'all': '1', 'filters': filters})) or []
        if not rows:
            raise KeyError(run_id)
        return rows[0]

    def get_run(self, run_id: str) -> dict[str, Any]:
        c = self._find_run_container(run_id)
        info = self._request('GET', f'/containers/{quote(c.get("Id", ""), safe="")}/json') or {}
        labels = ((info.get('Config') or {}).get('Labels') or {})
        state = info.get('State') or {}
        status = state.get('Status') or c.get('State') or 'unknown'
        exit_code = state.get('ExitCode')
        return {
            'run_id': run_id,
            'container_id': c.get('Id', ''),
            'id': str(c.get('Id', ''))[:12],
            'name': (c.get('Names') or ['/unknown'])[0].lstrip('/'),
            'agent_id': labels.get('com.senda.agent.template-id') or labels.get('com.senda.agent.id'),
            'workflow_id': labels.get(WORKFLOW_ID_LABEL),
            'parent_agent_id': labels.get(PARENT_AGENT_LABEL),
            'status': status,
            'running': bool(state.get('Running')),
            'exit_code': exit_code,
            'started_at': state.get('StartedAt'),
            'finished_at': state.get('FinishedAt'),
        }

    def list_runs(self, workflow_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        label_filters = [f'{RUNTIME_KIND_LABEL}=run']
        if workflow_id:
            label_filters.append(f'{WORKFLOW_ID_LABEL}={workflow_id}')
        filters = json.dumps({'label': label_filters})
        rows = self._request('GET', '/containers/json?' + urlencode({'all': '1', 'filters': filters})) or []
        out: list[dict[str, Any]] = []
        for c in rows[:max(1, min(int(limit), 500))]:
            labels = c.get('Labels') or {}
            out.append({
                'run_id': labels.get(RUN_ID_LABEL),
                'container_id': c.get('Id', ''),
                'id': str(c.get('Id', ''))[:12],
                'name': (c.get('Names') or ['/unknown'])[0].lstrip('/'),
                'agent_id': labels.get('com.senda.agent.template-id') or labels.get('com.senda.agent.id'),
                'workflow_id': labels.get(WORKFLOW_ID_LABEL),
                'status': c.get('State') or 'unknown',
            })
        return out

    def wait_run(self, run_id: str, timeout: float = 300) -> dict[str, Any]:
        c = self._find_run_container(run_id)
        cid = c.get('Id', '')
        self._request(
            'POST',
            f'/containers/{quote(cid, safe="")}/wait?condition=not-running',
            ok=(200,),
            timeout=max(1.0, float(timeout)),
        )
        return self.get_run(run_id)

    def run_logs(self, run_id: str, tail: int = 1000) -> str:
        c = self._find_run_container(run_id)
        return self.logs(c.get('Id', ''), tail=tail)

    @staticmethod
    def parse_result_from_logs(logs: str) -> Any:
        """Extract a structured result when an Agent follows the result marker contract.

        Supported line forms:
          [senda-agent-result] {"key":"value"}
          SENDA_AGENT_RESULT={"key":"value"}
        Otherwise the full logs are returned as result_text.
        """
        for line in reversed((logs or '').splitlines()):
            payload = None
            if '[senda-agent-result]' in line:
                payload = line.split('[senda-agent-result]', 1)[1].strip()
            elif 'SENDA_AGENT_RESULT=' in line:
                payload = line.split('SENDA_AGENT_RESULT=', 1)[1].strip()
            if payload:
                try:
                    return json.loads(payload)
                except Exception:
                    return payload
        return {'result_text': logs}

    def run_result(self, run_id: str) -> dict[str, Any]:
        state = self.get_run(run_id)
        logs = self.run_logs(run_id)
        return {**state, 'logs': logs, 'result': self.parse_result_from_logs(logs)}

    def stop_run(self, run_id: str) -> None:
        c = self._find_run_container(run_id)
        cid = c.get('Id', '')
        info = self._request('GET', f'/containers/{quote(cid, safe="")}/json') or {}
        if (info.get('State') or {}).get('Running'):
            self._request('POST', f'/containers/{quote(cid, safe="")}/stop?t=3', ok=(204, 304), timeout=15)

    def remove_run(self, run_id: str, force: bool = True) -> None:
        c = self._find_run_container(run_id)
        cid = c.get('Id', '')
        self._request(
            'DELETE',
            f'/containers/{quote(cid, safe="")}?v=0&force={1 if force else 0}',
            ok=(204,),
            timeout=15,
        )
