"""Connection check and collector canary for the Senda-Argus onboarding screen.

``send_connection_check(token)`` posts the one-time value issued by the admin API once,
through the same ``/v1/agent-runs/ingest`` endpoint that carries ordinary events.

``start_canary(secret=...)`` starts a daemon thread that posts a signed canary at a fixed
interval. The server raises a collection-stopped alert when the canaries from an agent stop.

**A canary must mean that collection works, not only that the process is alive.** Each beat
is handed to the Argus exporter that carries ordinary events, and is skipped while no
instrumentor is active or no Argus exporter is configured. ``shutdown()`` and
``unregister()`` stop the sender. If the exporter's queue is stuck, the canaries stop with it.

Neither path goes through the event bus. The bus writes to every configured exporter and
applies redaction, so a JSONL exporter would keep the one-time value on disk and redaction
could rewrite the signed fields.

Neither function raises. Observability must not stop the agent.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import importlib
import json
import os
import secrets
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from senda_argus_hooks.core.event import new_event
from senda_argus_hooks.core.runtime import effective_agent_id, get_bus, get_config

CONNECTION_CHECK_EVENT = "onboarding.connection_check"
CANARY_EVENT = "collector.canary"
CANARY_INTERVAL_DEFAULT = 60
CANARY_INTERVAL_MIN = 10
CANARY_INTERVAL_MAX = 3600
_MAX_SEQ = 2**53


def _mac(key: bytes, *parts: str) -> str:
    # Lengths are UTF-8 byte counts, the same on the server and the JS hooks.
    message = b"".join(
        str(len(p.encode("utf-8"))).encode("ascii") + b":" + p.encode("utf-8") + b"\n" for p in parts
    )
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def canary_mac(
    secret: str, *, agent_id: str, boot_id: str, seq: int, interval_sec: int, generation: int
) -> str:
    """The value the server recomputes for one canary.

    The server holds the same function. The tests on both sides pin the same inputs to the
    same expected value, so a change on one side alone fails a test instead of silently
    rejecting every canary.
    """
    return _mac(
        secret.encode("utf-8"),
        "canary-beat", agent_id, boot_id, str(seq), str(interval_sec), str(generation),
    )


def secret_generation(secret: str) -> int | None:
    """The generation the server put in front of the secret, ``cs<gen>.<value>``."""
    head, _, rest = secret.partition(".")
    if not rest or not head.startswith("cs") or not head[2:].isdigit():
        return None
    return int(head[2:])


def _argus_exporter():
    from senda_argus_hooks.exporters.argus import ArgusExporter

    for exporter in getattr(get_bus(), "exporters", []) or []:
        if isinstance(exporter, ArgusExporter):
            return exporter
    return None


def _collection_active() -> bool:
    """Whether instrumentation is installed and events have somewhere to go in Argus."""
    # パッケージの register は同名の関数で覆われるため、モジュールを名前で引く。
    _register = importlib.import_module("senda_argus_hooks.register")

    return bool(_register._ACTIVE_INSTRUMENTORS) and _argus_exporter() is not None


def _endpoint(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/") + "/v1/agent-runs/ingest"
    exporter = _argus_exporter()
    if exporter is not None:
        return exporter._url
    base = os.getenv("SENDA_ARGUS_ENDPOINT") or "http://localhost:8000"
    return base.rstrip("/") + "/v1/agent-runs/ingest"


def _api_key(explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    exporter = _argus_exporter()
    if exporter is not None:
        return exporter._api_key
    return os.getenv("SENDA_ARGUS_API_KEY", "")


def _build_event(event_type: str, data: dict[str, Any], agent_id: str) -> dict[str, Any]:
    cfg = get_config()
    return new_event(
        project=cfg.project,
        environment=cfg.environment,
        event_type=event_type,
        source={"component": "onboarding", "sdk": "senda_argus_hooks"},
        data=data,
        status="success",
        run_id=cfg.run_id or f"run_onboarding_{secrets.token_hex(8)}",
        agent_id=agent_id,
    ).to_dict()


def _post(url: str, api_key: str, events: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
    payload = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
    parsed = json.loads(body.decode("utf-8") or "{}")
    return parsed if isinstance(parsed, dict) else {}


def send_connection_check(
    token: str,
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    agent_id: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send the one-time connection check once and return the server's verdict.

    Returns ``{"status": "verified" | "rejected", "reason": ..., "check_id": ...}``, or
    ``{"status": "error", "reason": ...}`` when the request did not complete. The token is
    never logged.
    """
    if not isinstance(token, str) or not token.strip():
        return {"status": "error", "reason": "empty_token"}
    agent = agent_id or effective_agent_id()
    event = _build_event(CONNECTION_CHECK_EVENT, {"token": token.strip()}, agent)
    try:
        body = _post(_endpoint(endpoint), _api_key(api_key), [event], timeout)
    except urllib.error.HTTPError as exc:
        return {"status": "error", "reason": f"http_{exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": exc.__class__.__name__}
    results = body.get("connection_checks")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return dict(results[0])
    return {"status": "error", "reason": "no_result"}


class _Canary:
    def __init__(self, *, secret: str, generation: int, interval_sec: int, agent_id: str | None) -> None:
        self._secret = secret
        self._generation = generation
        self._interval = interval_sec
        # 明示されなければ、始めた時点の実効の識別子に固定する。
        self._derived = agent_id is None
        self._agent = agent_id if agent_id is not None else effective_agent_id()
        # A new boot id per process start. The server rejects a sequence number that does not
        # advance within the same boot, so a captured canary cannot be replayed.
        self._boot = secrets.token_hex(16)
        self._seq = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="senda-argus-canary", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def beat(self) -> bool:
        """Hand one canary to the Argus exporter. Returns False when collection is not active."""
        if self._stop.is_set() or not _collection_active():
            return False
        exporter = _argus_exporter()
        if exporter is None:
            return False
        seq = self._seq
        self._seq = (self._seq + 1) % _MAX_SEQ
        # 鍵はエージェントの識別子に束ねてある。識別子が始めた時点から変わったら送らない。新しい
        # 識別子で送っても署名が合わず、古い識別子で送り続けると止まった収集を生きて見せる。送らなければ
        # 古い側が途絶えの警報になり、鍵の発行し直しが要ると分かる。
        agent = effective_agent_id() if self._derived else self._agent
        if agent != self._agent:
            return False
        data = {
            "boot_id": self._boot,
            "seq": seq,
            "interval_sec": self._interval,
            "gen": self._generation,
            "mac": canary_mac(
                self._secret,
                agent_id=agent,
                boot_id=self._boot,
                seq=seq,
                interval_sec=self._interval,
                generation=self._generation,
            ),
        }
        try:
            exporter.export([_build_event(CANARY_EVENT, data, agent)])
            return True
        except Exception:  # noqa: BLE001
            return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.beat()
            if self._stop.wait(self._interval):
                return


_canary_lock = threading.Lock()
_canary: _Canary | None = None


def start_canary(
    *,
    secret: str | None = None,
    interval_sec: int | None = None,
    agent_id: str | None = None,
) -> bool:
    """Start sending signed canaries at a fixed interval. Returns True if started.

    ``secret`` defaults to ``SENDA_ARGUS_CANARY_SECRET`` and ``interval_sec`` to
    ``SENDA_ARGUS_CANARY_INTERVAL_SEC`` (60). The interval is clamped to [10, 3600], the range
    the server accepts. Starting twice keeps the first sender. The Argus exporter must use a
    collector key bound to this agent; the server ignores canaries sent with other keys.
    """
    global _canary
    key = secret if secret is not None else os.getenv("SENDA_ARGUS_CANARY_SECRET", "")
    generation = secret_generation(key) if key else None
    if generation is None:
        return False
    if interval_sec is None:
        try:
            interval_sec = int(os.getenv("SENDA_ARGUS_CANARY_INTERVAL_SEC", CANARY_INTERVAL_DEFAULT))
        except ValueError:
            interval_sec = CANARY_INTERVAL_DEFAULT
    interval = min(max(int(interval_sec), CANARY_INTERVAL_MIN), CANARY_INTERVAL_MAX)
    with _canary_lock:
        if _canary is not None:
            return False
        _canary = _Canary(
            secret=key,
            generation=generation,
            interval_sec=interval,
            agent_id=agent_id,
        )
        _canary.start()
    return True


def stop_canary() -> None:
    """Stop the canary sender. The server raises the collection-stopped alert afterwards."""
    global _canary
    with _canary_lock:
        if _canary is not None:
            _canary.stop()
        _canary = None


def _falsy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"0", "false", "no", "off", "n"}


def _check_marker(token: str) -> Path | None:
    try:
        base = Path(os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    except Exception:  # noqa: BLE001
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return base / "senda-argus" / f"connection-check-{digest}"


def start_from_env() -> None:
    """Called by the auto hook.

    The auto hook runs in every Python process on the host, including pip and helper scripts.
    The connection check is therefore sent once per host: a marker is left after the server
    answered, and later processes skip it.

    The canary starts when ``SENDA_ARGUS_CANARY_SECRET`` is set, which is how the onboarding
    screen configures the agent. Set ``SENDA_ARGUS_CANARY_AUTOSTART=false`` in the environment
    of processes that must not send it, such as batch jobs that end on purpose; otherwise each
    job end is reported as a collection stop.
    """
    token = os.getenv("SENDA_ARGUS_CONNECTION_CHECK_TOKEN", "").strip()
    if token:
        marker = _check_marker(token)
        if marker is None or not marker.exists():
            threading.Thread(
                target=_send_once, args=(token, marker), name="senda-argus-connection-check", daemon=True
            ).start()
    if not _falsy("SENDA_ARGUS_CANARY_AUTOSTART"):
        start_canary()


def restart_canary_from_env() -> None:
    """register の終わりに呼ぶ。鍵が設定され、止める指定が無ければ canary を始める。"""
    if not _falsy("SENDA_ARGUS_CANARY_AUTOSTART"):
        start_canary()


def _send_once(token: str, marker: Path | None) -> None:
    result = send_connection_check(token)
    if marker is None or result.get("status") == "error":
        return
    # The server answered. Verified or not, sending the same value again cannot change it.
    with contextlib.suppress(Exception):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(result.get("status") or ""), encoding="utf-8")
