from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                agent_id TEXT,
                run_id TEXT,
                trace_id TEXT,
                event_type TEXT NOT NULL,
                status TEXT,
                payload TEXT NOT NULL
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_agent_ts ON events(agent_id,timestamp DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_trace_ts ON events(trace_id,timestamp)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_run_ts ON events(run_id,timestamp)")

            c.execute("""CREATE TABLE IF NOT EXISTS agent_profiles (
                agent_id TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '[]',
                tags TEXT NOT NULL DEFAULT '[]',
                input_schema TEXT NOT NULL DEFAULT '{}',
                output_schema TEXT NOT NULL DEFAULT '{}',
                risk_level TEXT NOT NULL DEFAULT 'low',
                requires_approval INTEGER NOT NULL DEFAULT 0,
                allowed_callers TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )""")

            c.execute("""CREATE TABLE IF NOT EXISTS workflows (
                workflow_id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                initial_input TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                max_steps INTEGER NOT NULL DEFAULT 8,
                allowed_agents TEXT NOT NULL DEFAULT '[]',
                entry_agent_id TEXT,
                llm_config TEXT NOT NULL DEFAULT '{}',
                current_step INTEGER NOT NULL DEFAULT 0,
                pending_agent_id TEXT,
                pending_input TEXT,
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_workflows_updated ON workflows(updated_at DESC)")

            c.execute("""CREATE TABLE IF NOT EXISTS workflow_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                step_no INTEGER NOT NULL,
                agent_id TEXT,
                agent_run_id TEXT,
                action TEXT NOT NULL,
                reason TEXT,
                status TEXT NOT NULL,
                input TEXT,
                output TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workflow_id, step_no)
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_workflow_steps_wf ON workflow_steps(workflow_id,step_no)")

    # ---- Hook event storage -------------------------------------------------

    def add_many(self, events: list[dict[str, Any]]) -> int:
        rows = []
        for e in events:
            eid = str(e.get('event_id') or '')
            if not eid or not e.get('event_type'):
                continue
            rows.append((
                eid,
                str(e.get('timestamp') or ''),
                e.get('agent_id'),
                e.get('run_id'),
                e.get('trace_id'),
                str(e.get('event_type')),
                e.get('status'),
                json.dumps(e, ensure_ascii=False, default=str),
            ))
        if not rows:
            return 0
        with self._lock, self._conn() as c:
            before = c.total_changes
            c.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)", rows)
            return c.total_changes - before

    def recent(self, agent_id: str | None = None, limit: int = 100):
        limit = max(1, min(limit, 1000))
        with self._conn() as c:
            if agent_id:
                rs = c.execute(
                    "SELECT payload FROM events WHERE agent_id=? ORDER BY timestamp DESC LIMIT ?",
                    (agent_id, limit),
                ).fetchall()
            else:
                rs = c.execute("SELECT payload FROM events ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r['payload']) for r in rs]

    def trace(self, trace_id: str | None = None, run_id: str | None = None, agent_id: str | None = None, limit: int = 500):
        limit = max(1, min(limit, 2000))
        where: list[str] = []
        args: list[Any] = []
        if trace_id:
            where.append('trace_id=?')
            args.append(trace_id)
        if run_id:
            where.append('run_id=?')
            args.append(run_id)
        if agent_id:
            where.append('agent_id=?')
            args.append(agent_id)
        sql = 'SELECT payload FROM events'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY timestamp ASC LIMIT ?'
        args.append(limit)
        with self._conn() as c:
            rs = c.execute(sql, args).fetchall()
        return [json.loads(r['payload']) for r in rs]

    def trace_groups(self, agent_id: str, limit: int = 30):
        limit = max(1, min(limit, 100))
        with self._conn() as c:
            rs = c.execute(
                """
                SELECT trace_id, run_id, MAX(timestamp) AS last_timestamp, COUNT(*) AS event_count
                FROM events WHERE agent_id=? AND (trace_id IS NOT NULL OR run_id IS NOT NULL)
                GROUP BY trace_id, run_id ORDER BY last_timestamp DESC LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [dict(r) for r in rs]

    @staticmethod
    def _dig(event: dict[str, Any], *keys: str):
        scopes = [event]
        for name in ('attributes', 'metadata', 'request', 'response', 'data', 'details'):
            v = event.get(name)
            if isinstance(v, dict):
                scopes.append(v)
        for scope in scopes:
            for key in keys:
                value = scope.get(key)
                if value not in (None, '', [], {}):
                    return value
        return None

    @staticmethod
    def _timestamp_ms(value: Any) -> float | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp() * 1000
        except Exception:
            return None

    def trace_detail(self, trace_id: str | None = None, run_id: str | None = None, agent_id: str | None = None, limit: int = 500):
        events = self.trace(trace_id, run_id, agent_id, limit)
        model = prompt = response = None
        latency_ms = None
        for ev in events:
            model = model or self._dig(ev, 'model', 'model_name')
            if prompt is None:
                prompt = self._dig(ev, 'prompt', 'input', 'messages', 'request_body')
            if response is None:
                response = self._dig(ev, 'response', 'output', 'content', 'text', 'result')
            if latency_ms is None:
                latency_ms = self._dig(ev, 'latency_ms', 'duration_ms', 'elapsed_ms')
        if latency_ms is None and len(events) > 1:
            start = self._timestamp_ms(events[0].get('timestamp'))
            end = self._timestamp_ms(events[-1].get('timestamp'))
            if start is not None and end is not None and end >= start:
                latency_ms = round(end - start, 3)
        summary = {
            'agent_id': agent_id or (events[0].get('agent_id') if events else None),
            'trace_id': trace_id or (events[0].get('trace_id') if events else None),
            'run_id': run_id or (events[0].get('run_id') if events else None),
            'model': model,
            'prompt': prompt,
            'response': response,
            'latency_ms': latency_ms,
            'event_count': len(events),
            'status': next((e.get('status') for e in reversed(events) if e.get('status')), None),
        }
        return {'summary': summary, 'events': events}

    # ---- Agent registry metadata ------------------------------------------

    def upsert_agent_profile(self, agent_id: str, profile: dict[str, Any]) -> None:
        row = (
            agent_id,
            str(profile.get('description') or ''),
            json.dumps(profile.get('capabilities') or [], ensure_ascii=False),
            json.dumps(profile.get('tags') or [], ensure_ascii=False),
            json.dumps(profile.get('input_schema') or {}, ensure_ascii=False),
            json.dumps(profile.get('output_schema') or {}, ensure_ascii=False),
            str(profile.get('risk_level') or 'low'),
            1 if profile.get('requires_approval') else 0,
            json.dumps(profile.get('allowed_callers') or [], ensure_ascii=False),
            utcnow(),
        )
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO agent_profiles(
                    agent_id,description,capabilities,tags,input_schema,output_schema,
                    risk_level,requires_approval,allowed_callers,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    description=excluded.description,
                    capabilities=excluded.capabilities,
                    tags=excluded.tags,
                    input_schema=excluded.input_schema,
                    output_schema=excluded.output_schema,
                    risk_level=excluded.risk_level,
                    requires_approval=excluded.requires_approval,
                    allowed_callers=excluded.allowed_callers,
                    updated_at=excluded.updated_at
                """,
                row,
            )

    def get_agent_profile(self, agent_id: str) -> dict[str, Any]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
        if not r:
            return {
                'agent_id': agent_id,
                'description': '',
                'capabilities': [],
                'tags': [],
                'input_schema': {},
                'output_schema': {},
                'risk_level': 'low',
                'requires_approval': False,
                'allowed_callers': [],
            }
        d = dict(r)
        for key in ('capabilities', 'tags', 'input_schema', 'output_schema', 'allowed_callers'):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                d[key] = [] if key in ('capabilities', 'tags', 'allowed_callers') else {}
        d['requires_approval'] = bool(d['requires_approval'])
        return d

    def delete_agent_profile(self, agent_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM agent_profiles WHERE agent_id=?", (agent_id,))

    def merge_agent_profiles(self, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**a, **self.get_agent_profile(str(a.get('agent_id') or a.get('name') or ''))} for a in agents]

    # ---- Workflow state ---------------------------------------------------

    def create_workflow(
        self,
        goal: str,
        initial_input: dict[str, Any] | None = None,
        max_steps: int = 8,
        allowed_agents: list[str] | None = None,
        entry_agent_id: str | None = None,
        llm_config: dict[str, Any] | None = None,
        status: str = 'queued',
    ) -> dict[str, Any]:
        workflow_id = 'wf_' + uuid.uuid4().hex[:20]
        now = utcnow()
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO workflows(
                    workflow_id,goal,initial_input,status,max_steps,allowed_agents,
                    entry_agent_id,llm_config,current_step,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    workflow_id,
                    goal,
                    json.dumps(initial_input or {}, ensure_ascii=False),
                    str(status or 'queued'),
                    int(max_steps),
                    json.dumps(allowed_agents or [], ensure_ascii=False),
                    entry_agent_id,
                    json.dumps(llm_config or {}, ensure_ascii=False),
                    0,
                    now,
                    now,
                ),
            )
        return self.get_workflow(workflow_id)

    def reset_workflow(self, workflow_id: str, status: str = 'queued') -> dict[str, Any]:
        now = utcnow()
        with self._lock, self._conn() as c:
            exists = c.execute('SELECT 1 FROM workflows WHERE workflow_id=?', (workflow_id,)).fetchone()
            if not exists:
                raise KeyError(workflow_id)
            c.execute('DELETE FROM workflow_steps WHERE workflow_id=?', (workflow_id,))
            c.execute(
                '''UPDATE workflows SET status=?,current_step=0,pending_agent_id=NULL,pending_input=NULL,
                   result=NULL,error=NULL,updated_at=? WHERE workflow_id=?''',
                (status, now, workflow_id),
            )
        return self.get_workflow(workflow_id)

    def delete_workflow(self, workflow_id: str) -> None:
        with self._lock, self._conn() as c:
            exists = c.execute('SELECT 1 FROM workflows WHERE workflow_id=?', (workflow_id,)).fetchone()
            if not exists:
                raise KeyError(workflow_id)
            c.execute('DELETE FROM workflow_steps WHERE workflow_id=?', (workflow_id,))
            c.execute('DELETE FROM workflows WHERE workflow_id=?', (workflow_id,))

    def update_workflow(self, workflow_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            'status', 'current_step', 'pending_agent_id', 'pending_input',
            'result', 'error', 'llm_config', 'allowed_agents', 'entry_agent_id',
        }
        sets: list[str] = []
        args: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {'pending_input', 'result', 'llm_config', 'allowed_agents'} and value is not None and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, default=str)
            sets.append(f'{key}=?')
            args.append(value)
        sets.append('updated_at=?')
        args.append(utcnow())
        args.append(workflow_id)
        with self._lock, self._conn() as c:
            cur = c.execute(f"UPDATE workflows SET {', '.join(sets)} WHERE workflow_id=?", args)
            if cur.rowcount == 0:
                raise KeyError(workflow_id)
        return self.get_workflow(workflow_id)

    def add_workflow_step(
        self,
        workflow_id: str,
        step_no: int,
        action: str,
        status: str,
        agent_id: str | None = None,
        agent_run_id: str | None = None,
        reason: str | None = None,
        input_data: Any = None,
        output_data: Any = None,
    ) -> dict[str, Any]:
        now = utcnow()
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO workflow_steps(
                    workflow_id,step_no,agent_id,agent_run_id,action,reason,status,input,output,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workflow_id,step_no) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    agent_run_id=excluded.agent_run_id,
                    action=excluded.action,
                    reason=excluded.reason,
                    status=excluded.status,
                    input=excluded.input,
                    output=excluded.output,
                    updated_at=excluded.updated_at
                """,
                (
                    workflow_id,
                    int(step_no),
                    agent_id,
                    agent_run_id,
                    action,
                    reason,
                    status,
                    json.dumps(input_data, ensure_ascii=False, default=str) if input_data is not None else None,
                    json.dumps(output_data, ensure_ascii=False, default=str) if output_data is not None else None,
                    now,
                    now,
                ),
            )
        return self.get_workflow_step(workflow_id, step_no)

    def update_workflow_step(self, workflow_id: str, step_no: int, **fields: Any) -> dict[str, Any]:
        allowed = {'agent_id', 'agent_run_id', 'action', 'reason', 'status', 'input', 'output'}
        sets: list[str] = []
        args: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {'input', 'output'} and value is not None and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, default=str)
            sets.append(f'{key}=?')
            args.append(value)
        sets.append('updated_at=?')
        args.append(utcnow())
        args.extend([workflow_id, int(step_no)])
        with self._lock, self._conn() as c:
            cur = c.execute(
                f"UPDATE workflow_steps SET {', '.join(sets)} WHERE workflow_id=? AND step_no=?",
                args,
            )
            if cur.rowcount == 0:
                raise KeyError((workflow_id, step_no))
        return self.get_workflow_step(workflow_id, step_no)

    def get_workflow_step(self, workflow_id: str, step_no: int) -> dict[str, Any]:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND step_no=?",
                (workflow_id, int(step_no)),
            ).fetchone()
        if not r:
            raise KeyError((workflow_id, step_no))
        return self._decode_workflow_step(dict(r))

    def workflow_steps(self, workflow_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rs = c.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY step_no ASC",
                (workflow_id,),
            ).fetchall()
        return [self._decode_workflow_step(dict(r)) for r in rs]

    @staticmethod
    def _decode_workflow_step(d: dict[str, Any]) -> dict[str, Any]:
        for key in ('input', 'output'):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except Exception:
                    pass
        return d

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
        if not r:
            raise KeyError(workflow_id)
        d = dict(r)
        for key in ('initial_input', 'allowed_agents', 'llm_config', 'pending_input', 'result'):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except Exception:
                    pass
            elif key in ('initial_input', 'llm_config'):
                d[key] = {}
            elif key == 'allowed_agents':
                d[key] = []
        d['steps'] = self.workflow_steps(workflow_id)
        return d

    def list_workflows(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._conn() as c:
            rs = c.execute(
                "SELECT workflow_id FROM workflows ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self.get_workflow(r['workflow_id']) for r in rs]
