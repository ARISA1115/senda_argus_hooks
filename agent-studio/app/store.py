from __future__ import annotations
import json, sqlite3, threading
from datetime import datetime
from pathlib import Path
from typing import Any


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

    def add_many(self, events: list[dict[str, Any]]) -> int:
        rows=[]
        for e in events:
            eid=str(e.get('event_id') or '')
            if not eid or not e.get('event_type'):
                continue
            rows.append((eid, str(e.get('timestamp') or ''), e.get('agent_id'), e.get('run_id'), e.get('trace_id'), str(e.get('event_type')), e.get('status'), json.dumps(e, ensure_ascii=False, default=str)))
        if not rows: return 0
        with self._lock, self._conn() as c:
            before=c.total_changes
            c.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)", rows)
            return c.total_changes-before

    def recent(self, agent_id: str|None=None, limit:int=100):
        limit=max(1,min(limit,1000))
        with self._conn() as c:
            if agent_id:
                rs=c.execute("SELECT payload FROM events WHERE agent_id=? ORDER BY timestamp DESC LIMIT ?",(agent_id,limit)).fetchall()
            else:
                rs=c.execute("SELECT payload FROM events ORDER BY timestamp DESC LIMIT ?",(limit,)).fetchall()
        return [json.loads(r['payload']) for r in rs]

    def trace(self, trace_id:str|None=None, run_id:str|None=None, agent_id:str|None=None, limit:int=500):
        limit=max(1,min(limit,2000)); where=[]; args=[]
        if trace_id: where.append('trace_id=?'); args.append(trace_id)
        if run_id: where.append('run_id=?'); args.append(run_id)
        if agent_id: where.append('agent_id=?'); args.append(agent_id)
        sql='SELECT payload FROM events'
        if where: sql+=' WHERE '+' AND '.join(where)
        sql+=' ORDER BY timestamp ASC LIMIT ?'; args.append(limit)
        with self._conn() as c: rs=c.execute(sql,args).fetchall()
        return [json.loads(r['payload']) for r in rs]

    def trace_groups(self, agent_id: str, limit: int = 30):
        limit=max(1,min(limit,100))
        with self._conn() as c:
            rs=c.execute("""
                SELECT trace_id, run_id, MAX(timestamp) AS last_timestamp, COUNT(*) AS event_count
                FROM events WHERE agent_id=? AND (trace_id IS NOT NULL OR run_id IS NOT NULL)
                GROUP BY trace_id, run_id ORDER BY last_timestamp DESC LIMIT ?
            """,(agent_id,limit)).fetchall()
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

    def trace_detail(self, trace_id:str|None=None, run_id:str|None=None, agent_id:str|None=None, limit:int=500):
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
