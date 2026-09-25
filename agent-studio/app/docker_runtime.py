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
            state = c.get('State') or 'unknown'
            mounts = c.get('Mounts') or []
            source_mount = next((m for m in mounts if m.get('Destination') == labels.get('com.senda.agent.container-path', '/workspace')), None)
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
        }

        host_config: dict[str, Any] = {
            'RestartPolicy': {'Name': spec.get('restart_policy', 'unless-stopped')},
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
        self.start(cid)
        return {'id': cid[:12], 'container_id': cid, 'name': name, 'agent_id': agent_id}
