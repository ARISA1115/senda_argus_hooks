from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import Any

from .store import EventStore

logger = logging.getLogger('uvicorn.error')


class EventBus:
    """Store, stream and optionally forward Agent Studio events.

    Internal control-plane events (workflow/supervisor/Jev) use the same path as
    hook events ingested from Agent runtimes so they are visible in Live Events
    and, when configured, forwarded to the upstream Senda Argus API.
    """

    def __init__(self, store: EventStore):
        self.store = store
        self.subscribers: set[asyncio.Queue] = set()

    async def publish(self, events: list[dict[str, Any]]) -> int:
        evs = [e for e in events if isinstance(e, dict)]
        accepted = self.store.add_many(evs)

        upstream = os.getenv('SENDA_STUDIO_ARGUS_UPSTREAM', '').rstrip('/')
        if upstream and evs:
            headers = {'Content-Type': 'application/json'}
            key = os.getenv('SENDA_STUDIO_ARGUS_API_KEY', '')
            if key:
                headers['X-API-Key'] = key
            payload = json.dumps({'events': evs}, ensure_ascii=False, default=str).encode()
            upstream_url = upstream + '/v1/agent-runs/ingest'
            logger.info('[senda-studio-argus] POST %s events=%d bytes=%d', upstream_url, len(evs), len(payload))

            def _forward() -> int:
                req = urllib.request.Request(upstream_url, data=payload, method='POST', headers=headers)
                with urllib.request.urlopen(req, timeout=5) as response:
                    return getattr(response, 'status', None) or response.getcode()

            try:
                status = await asyncio.to_thread(_forward)
                logger.info('[senda-studio-argus] POST completed status=%s url=%s events=%d', status, upstream_url, len(evs))
            except Exception as exc:
                logger.warning('[senda-studio-argus] POST failed url=%s events=%d error=%s', upstream_url, len(evs), exc)

        for e in evs:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(e)
                except asyncio.QueueFull:
                    pass
        return accepted

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
