"""Devin の API v3 からセッションと組織の監査記録を引き取り、既存の事象へ写して送る。

Devin はクラウドの仮想機で動き、計装を差せない。外へ通知する口も無いため、API を周期的に引き取る。

写し方:

- セッションの開始は ``agent.run.started``、終わり (status が exit か error) は ``agent.run.completed``
  か ``agent.run.failed``。run_id はセッションの識別子にする
- 利用者がセッションへ送ったメッセージは ``llm.request`` の指示の欄。本文は送らず、指示ファイルの伝播の
  判定が読む行と組のダイジェストを他の計装と同じ関数で作る。本文は ``capture_prompt`` を有効にした
  ときだけ載せ、載せる値も送る前に秘匿を通す
- 組織の監査記録は ``agent.step.completed``。日ごとの実行 ``devin-audit:<組織>:<日付>`` に置く。MCP
  サーバの設定の変更は、そのサーバ名を ``configured_mcp_server`` に載せ、Argus の MCP サーバの一覧で
  中継を通らない未観測のサーバとして出す

**同じ記録を二度写さない。** 事象の識別子をセッションと記録の識別子から決定的に作る。受け取り側は
識別子で重複を除く。取得位置は送り先が受け取ったと答えてから進める。

**セッションの区間。** 中継の呼び出しはセッションの識別子を運ばない。終わったセッションについて、
同じ組織の他のセッションが動いていなかった区間 (``exclusive_windows``) を終わりの記録に載せる。
終わった時点までに作られたセッションは全て一覧に現れているため、その時点で区間は確定する。初回は
組織の全セッションを読む。読み切れなかったときと、控えから外したセッションと重なりうるときは、
区間を載せない。重なりを除けない区間で結びつけると、誤った帰属になる。

資格情報は環境変数から読み、記録にも送出にも載せない。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any, Final

from senda_argus_hooks.core.event import new_event
from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.instruction_files import (
    system_prompt_line_digests,
    system_prompt_pair_digests,
    system_prompt_semantic_digests,
)
from senda_argus_hooks.core.redaction import redact_event

logger = logging.getLogger("senda_argus_hooks.collectors.devin")

DEFAULT_API_BASE: Final[str] = "https://api.devin.ai"
SOURCE_NAME: Final[str] = "devin"
COMPONENT: Final[str] = "devin_collector"

# 終わりを表す status。API の SessionResponse.status の列挙のうち、再開しないもの。suspended は
# 再開しうるため終わりに数えない。
_ENDED_STATUSES: Final[frozenset[str]] = frozenset({"exit", "error"})

# 監査記録の action のうち、MCP サーバの設定に在ることを表すもの。名前は記録の data から読む。
_MCP_CONFIG_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "mcp_server_install",
        "mcp_server_update",
        "mcp_server_enable",
        "mcp_server_credential_set",
        "mcp_server_oauth_tokens_granted",
    }
)
# 監査記録の data のうち、サーバ名を運ぶ鍵の候補。公開の文書は data の中身を定めていないため、
# 名前を判別できない記録には名前を載せない。既定の名前へ倒さない。
_MCP_NAME_KEYS: Final[tuple[str, ...]] = (
    "mcp_server_name",
    "server_name",
    "mcp_name",
    "name",
)

# 1 回の引き取りで読む頁と件数の上限。API の first の上限は 200。
PAGE_SIZE: Final[int] = 100
MAX_PAGES: Final[int] = 50
MAX_MESSAGES_PER_SESSION: Final[int] = 1000
# 初回に遡る時間。これより前から動いていたセッションには区間を載せない。
DEFAULT_LOOKBACK_SEC: Final[int] = 7 * 86400
# 一覧の取得位置を戻す幅。取得と更新が同じ秒に重なっても取りこぼさない。
CURSOR_OVERLAP_SEC: Final[int] = 60
# 控えるセッションの数と期間。区間の計算に要る分だけ持つ。
MAX_KNOWN_SESSIONS: Final[int] = 5000
KNOWN_SESSION_RETENTION_SEC: Final[int] = 14 * 86400
# 本文を載せるときの長さの上限。
MAX_MESSAGE_CHARS: Final[int] = 32768
MAX_ID_LEN: Final[int] = 256

Fetch = Callable[[str, dict[str, str]], Any]


class DevinApiError(Exception):
    """API の取得に失敗した。資格情報と URL の値は載せない。"""


def _clean_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > MAX_ID_LEN or any(ord(c) < 0x20 for c in value):
        return ""
    return value


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _event_id(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"evt_devin_{digest}"


def _urllib_fetch(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise DevinApiError(f"Devin API returned {exc.code}") from None
    except (urllib.error.URLError, OSError):
        raise DevinApiError("Devin API is unreachable") from None
    if len(body) > 8 * 1024 * 1024:
        raise DevinApiError("Devin API response too large")
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise DevinApiError("Devin API returned a non-JSON body") from None


class DevinApi:
    """Devin の API v3 の読み取り。組織の範囲の API と、有効にしたときだけ企業の範囲の監査記録を読む。"""

    def __init__(
        self,
        *,
        api_key: str,
        org_id: str,
        base_url: str = DEFAULT_API_BASE,
        fetch: Fetch | None = None,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" and fetch is None:
            raise ValueError("Devin API base URL must be https")
        if not api_key:
            raise ValueError("Devin API key is required")
        self._key = api_key
        self._org = org_id
        self._base = base_url.rstrip("/")
        self._fetch = fetch or _urllib_fetch

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            [(k, v) for k, v in params.items() if v is not None], doseq=True
        )
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        body = self._fetch(url, {"Authorization": f"Bearer {self._key}"})
        if not isinstance(body, dict):
            raise DevinApiError("Devin API returned an unexpected body")
        return body

    def _pages(self, path: str, params: dict[str, Any], limit: int) -> Iterable[dict]:
        after: str | None = None
        count = 0
        for _ in range(MAX_PAGES):
            body = self._get(path, {**params, "first": PAGE_SIZE, "after": after})
            items = body.get("items")
            if not isinstance(items, list):
                raise DevinApiError("Devin API page has no items")
            for item in items:
                if isinstance(item, dict):
                    yield item
                    count += 1
                    if count >= limit:
                        return
            after = body.get("end_cursor") if body.get("has_next_page") else None
            if not isinstance(after, str) or not after:
                return

    def sessions(self, *, updated_after: int) -> list[dict[str, Any]]:
        org = urllib.parse.quote(self._org, safe="")
        return list(
            self._pages(
                f"/v3/organizations/{org}/sessions",
                {"updated_after": updated_after},
                PAGE_SIZE * MAX_PAGES,
            )
        )

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        org = urllib.parse.quote(self._org, safe="")
        sid = urllib.parse.quote(session_id, safe="")
        return list(
            self._pages(
                f"/v3/organizations/{org}/sessions/{sid}/messages",
                {},
                MAX_MESSAGES_PER_SESSION,
            )
        )

    def audit_logs(self, *, time_after: int) -> list[dict[str, Any]]:
        org = urllib.parse.quote(self._org, safe="")
        return list(
            self._pages(
                f"/v3/enterprise/organizations/{org}/audit-logs",
                {"time_after": time_after, "order": "asc"},
                PAGE_SIZE * MAX_PAGES,
            )
        )


def exclusive_windows(
    start: float, end: float, others: Iterable[tuple[float, float | None]]
) -> list[list[float]]:
    """[start, end] から、他のセッションが動いていた区間を除いた区間の一覧。

    終わっていない他のセッションは、終わりを無限とみなす。
    """
    cuts = sorted(
        (max(start, s), min(end, e if e is not None else end))
        for s, e in others
        if s <= end and (e is None or e >= start)
    )
    windows: list[list[float]] = []
    cursor = start
    for s, e in cuts:
        if s > cursor:
            windows.append([cursor, s])
        cursor = max(cursor, e)
        if cursor >= end:
            break
    if cursor < end:
        windows.append([cursor, end])
    return [w for w in windows if w[1] > w[0]]


class DevinCollector:
    """Devin の組織 1 つ分を引き取り、Argus へ送る。"""

    def __init__(
        self,
        api: DevinApi,
        *,
        org_id: str,
        send: Callable[[list[dict[str, Any]]], bool],
        state_path: str,
        agent_id: str = "",
        project: str = "devin",
        environment: str = "cloud",
        capture_prompt: bool = False,
        redact: bool = True,
        include_audit_logs: bool = False,
        clock: Callable[[], float] = time.time,
        lookback_sec: int = DEFAULT_LOOKBACK_SEC,
    ) -> None:
        org = _clean_id(org_id)
        if not org:
            raise ValueError("Devin organization id is required")
        self._api = api
        self._org = org
        self._send = send
        self._state_path = state_path
        self._agent_id = _clean_id(agent_id) or f"devin:{org}"
        self._project = project
        self._environment = environment
        self._capture_prompt = capture_prompt
        self._redact = redact
        self._audit = include_audit_logs
        self._clock = clock
        self._lookback = lookback_sec

    # ----- 状態
    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self._state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            if isinstance(state, dict) and state.get("org_id") == self._org:
                return state
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            logger.warning("Devin の収集の状態を読めません。初めから引き取ります。")
        return {"org_id": self._org}

    def _save_state(self, state: dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self._state_path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".devin_state.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, self._state_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ----- 事象
    def _event(
        self,
        event_type: str,
        *,
        event_id: str,
        run_id: str,
        epoch: float,
        data: dict[str, Any],
        status: str | None = "success",
    ) -> dict[str, Any]:
        ev = new_event(
            project=self._project,
            environment=self._environment,
            event_type=event_type,
            trace_id=run_id,
            source={"component": COMPONENT, "provider": SOURCE_NAME},
            data=data,
            security={"redacted": False},
            status=status,
            run_id=run_id,
            agent_id=self._agent_id,
        ).to_dict()
        ev["event_id"] = event_id
        ev["timestamp"] = _iso(epoch)
        return redact_event(ev) if self._redact else ev

    def _session_block(self, session: dict[str, Any]) -> dict[str, Any]:
        block: dict[str, Any] = {
            "org_id": self._org,
            "session_id": session["session_id"],
            "status": str(session.get("status") or ""),
            # 中継を通らない MCP サーバは提示も呼び出しも見えない。監査記録を引かない構成では
            # 設定も読めないため、見えている範囲が中継だけであることを記録に残す。
            "mcp_visibility": "relay_and_audit_log" if self._audit else "relay_only",
        }
        for key in ("origin", "playbook_id", "automation_id", "parent_session_id"):
            value = _clean_id(session.get(key))
            if value:
                block[key] = value
        return block

    def _message_event(
        self, session_id: str, message: dict[str, Any]
    ) -> dict[str, Any] | None:
        if message.get("source") != "user":
            return None
        message_id = _clean_id(message.get("event_id"))
        created = _epoch(message.get("created_at"))
        text = message.get("message")
        if not message_id or created is None or not isinstance(text, str):
            return None
        llm: dict[str, Any] = {
            "provider": SOURCE_NAME,
            "operation": "session.message",
        }
        line_hashes = system_prompt_line_digests(instructions=text)
        pair_hashes = system_prompt_pair_digests(instructions=text)
        if line_hashes:
            llm["system_prompt_line_hashes"] = line_hashes
        if pair_hashes:
            llm["system_prompt_pair_hashes"] = pair_hashes
        # 日本語だけの指示は組を作れない。文の署名はテナントの鍵と埋め込みが設定されたときだけ出る。
        semantic_hashes = system_prompt_semantic_digests(instructions=text)
        if semantic_hashes:
            llm["system_prompt_semantic_hashes"] = semantic_hashes
        if self._capture_prompt:
            llm["input"] = {"instructions": text[:MAX_MESSAGE_CHARS]}
        else:
            llm["input"] = {"input_hash": sha256_value(text)}
        return self._event(
            "llm.request",
            event_id=_event_id(self._org, session_id, "message", message_id),
            run_id=session_id,
            epoch=created,
            data={"llm": llm, "devin": {"org_id": self._org, "session_id": session_id}},
        )

    def _audit_event(self, item: dict[str, Any]) -> dict[str, Any] | None:
        log_id = _clean_id(item.get("audit_log_id"))
        action = _clean_id(item.get("action"))
        created = _epoch(item.get("created_at"))
        if not log_id or not action or created is None:
            return None
        block: dict[str, Any] = {
            "org_id": self._org,
            "audit_log_id": log_id,
            "audit_action": action,
        }
        # 利用者のメールは送らない。主体は識別子だけを載せる。
        for key in ("user_id", "service_user_id"):
            value = _clean_id(item.get(key))
            if value:
                block[key] = value
        data = item.get("data")
        if action in _MCP_CONFIG_ACTIONS and isinstance(data, dict):
            for key in _MCP_NAME_KEYS:
                name = _clean_id(data.get(key))
                if name:
                    block["configured_mcp_server"] = name
                    break
        day = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y%m%d")
        return self._event(
            "agent.step.completed",
            event_id=_event_id(self._org, "audit", log_id),
            run_id=f"devin-audit:{self._org}:{day}",
            epoch=created,
            data={"devin": block},
        )

    # ----- 引き取り
    def poll_once(self) -> dict[str, int]:
        """1 回引き取る。送れた記録の数を返す。送れなかったときは取得位置を進めない。"""
        now = self._clock()
        state = self._load_state()
        first = "complete_since" not in state
        known: dict[str, dict[str, Any]] = dict(state.get("sessions") or {})
        sent = 0

        if first:
            # 初回は組織の全セッションを読み、重なりの相手を漏れなく控える。上限で読み切れなかった
            # ときは、この時刻より前に始まったセッションに区間を載せない。
            listed = self._api.sessions(updated_after=0)
            complete_since = 0.0 if len(listed) < PAGE_SIZE * MAX_PAGES else now
            cursor = 0
        else:
            complete_since = float(state["complete_since"])
            cursor = int(state.get("sessions_cursor") or 0)
            listed = self._api.sessions(
                updated_after=max(0, cursor - CURSOR_OVERLAP_SEC)
            )
            if len(listed) >= PAGE_SIZE * MAX_PAGES:
                # 上限で読み切れなかった。取りこぼしたセッションは重なりの相手から抜けるため、
                # この時刻より前に始まったセッションには区間を載せない。
                complete_since = max(complete_since, now)
        state["complete_since"] = complete_since
        for session in listed:
            sid = _clean_id(session.get("session_id"))
            created = _epoch(session.get("created_at"))
            updated = _epoch(session.get("updated_at"))
            if not sid or created is None:
                continue
            ended = str(session.get("status") or "") in _ENDED_STATUSES
            entry = known.setdefault(sid, {"created_at": created})
            entry["created_at"] = created
            entry["updated_at"] = updated if updated is not None else created
            entry["ended_at"] = (
                (updated if updated is not None else created) if ended else None
            )
            entry["status"] = str(session.get("status") or "")
            entry["block"] = self._session_block({**session, "session_id": sid})
            if first and ended and entry["updated_at"] < now - self._lookback:
                # 遡る期間より前に終わったセッションは送らず、重なりの相手としてだけ控える。
                entry["started_sent"] = True
                entry["completed_sent"] = True
                entry["messages_seen_at"] = entry["updated_at"]

        for sid, entry in sorted(known.items(), key=lambda kv: kv[1]["created_at"]):
            batch: list[dict[str, Any]] = []
            if not entry.get("started_sent"):
                batch.append(
                    self._event(
                        "agent.run.started",
                        event_id=_event_id(self._org, sid, "started"),
                        run_id=sid,
                        epoch=entry["created_at"],
                        data={
                            "devin": {
                                **entry["block"],
                                "created_at": entry["created_at"],
                            }
                        },
                    )
                )
            if not entry.get("completed_sent") and (
                entry.get("updated_at") != entry.get("messages_seen_at")
                or entry.get("ended_at") is not None
            ):
                messages = self._api.messages(sid)
                if len(messages) >= MAX_MESSAGES_PER_SESSION:
                    # 上限で読み切れなかった。終わりの記録に印を残す。
                    entry["messages_truncated"] = True
                for message in messages:
                    ev = self._message_event(sid, message)
                    if ev is not None:
                        batch.append(ev)
            completed = None
            if entry.get("ended_at") is not None and not entry.get("completed_sent"):
                # 区間を確定できるのは、重なりうる相手を全て控えている時刻より後に始まったセッション
                # だけである。控えから外したセッションが終わった時刻より前に始まったものは除く。
                known_since = max(
                    complete_since,
                    now - KNOWN_SESSION_RETENTION_SEC,
                    float(state.get("pruned_until") or 0.0),
                )
                completed = self._completion_event(sid, entry, known, known_since, now)
                batch.append(completed)
            if batch:
                if not self._send(batch):
                    # 送れなかった。位置を進めず、次の引き取りで同じ記録を送り直す。
                    self._save_state({**state, "sessions": known})
                    return {"sent": sent, "failed": 1}
                sent += len(batch)
            entry["started_sent"] = True
            entry["messages_seen_at"] = entry.get("updated_at")
            if completed is not None:
                entry["completed_sent"] = True

        if listed:
            newest = max((_epoch(s.get("updated_at")) or 0.0) for s in listed)
            state["sessions_cursor"] = int(max(cursor, newest))

        if self._audit:
            audit_cursor = int(state.get("audit_cursor") or complete_since)
            boundary = set(state.get("audit_boundary_ids") or [])
            # 取得位置の秒も引き直し、同じ秒に後から載った記録を取りこぼさない。その秒の既に送った
            # 記録は識別子で外し、同じ記録を二度送らない。
            items = [
                i
                for i in self._api.audit_logs(time_after=audit_cursor)
                if _clean_id(i.get("audit_log_id")) not in boundary
            ]
            events = [ev for ev in map(self._audit_event, items) if ev is not None]
            if events and not self._send(events):
                state["sessions"] = known
                self._save_state(state)
                return {"sent": sent, "failed": 1}
            sent += len(events)
            newest_audit = max(
                [audit_cursor] + [int(_epoch(i.get("created_at")) or 0) for i in items]
            )
            if newest_audit > audit_cursor:
                boundary = set()
            boundary |= {
                _clean_id(i.get("audit_log_id"))
                for i in items
                if int(_epoch(i.get("created_at")) or 0) == newest_audit
            }
            state["audit_cursor"] = newest_audit
            state["audit_boundary_ids"] = sorted(b for b in boundary if b)

        kept, pruned_until = self._prune(known, now)
        state["sessions"] = kept
        state["pruned_until"] = max(
            float(state.get("pruned_until") or 0.0), pruned_until
        )
        self._save_state(state)
        return {"sent": sent, "failed": 0}

    def _completion_event(
        self,
        sid: str,
        entry: dict[str, Any],
        known: dict[str, dict[str, Any]],
        complete_since: float,
        now: float,
    ) -> dict[str, Any]:
        start = float(entry["created_at"])
        end = float(entry["ended_at"])
        block: dict[str, Any] = {
            **entry["block"],
            "created_at": start,
            "ended_at": end,
            "observed_at": now,
        }
        if entry.get("messages_truncated"):
            block["messages_truncated"] = True
        if start >= complete_since:
            others = [
                (float(o["created_at"]), o.get("ended_at"))
                for other_id, o in known.items()
                if other_id != sid
            ]
            block["exclusive_windows"] = exclusive_windows(start, end, others)
        else:
            # 収集を始める前から動いていたセッションは、重なる相手を知り得ない。
            block["exclusive_windows"] = []
            block["attribution_unavailable"] = "started_before_collection"
        event_type = (
            "agent.run.failed"
            if entry.get("status") == "error"
            else "agent.run.completed"
        )
        return self._event(
            event_type,
            event_id=_event_id(self._org, sid, "completed"),
            run_id=sid,
            epoch=end,
            data={"devin": block},
            status="error" if event_type == "agent.run.failed" else "success",
        )

    @staticmethod
    def _prune(
        known: dict[str, dict[str, Any]], now: float
    ) -> tuple[dict[str, dict[str, Any]], float]:
        """終わってから保持の期間を過ぎたセッションを外す。件数の上限を超えたら古いものから外す。

        動いているセッションは外さない。外すと、その区間に重なる他のセッションの区間を誤る。上限で
        外したセッションの終わりの最も遅い時刻を返す。それより前に始まったセッションには区間を
        載せない。
        """
        kept = {
            sid: e
            for sid, e in known.items()
            if e.get("ended_at") is None
            or now - float(e["ended_at"]) <= KNOWN_SESSION_RETENTION_SEC
            or not e.get("completed_sent")
        }
        if len(kept) > MAX_KNOWN_SESSIONS:
            ended = sorted(
                (
                    sid
                    for sid, e in kept.items()
                    if e.get("ended_at") is not None and e.get("completed_sent")
                ),
                key=lambda sid: kept[sid]["ended_at"],
            )
            pruned_until = 0.0
            for sid in ended[: len(kept) - MAX_KNOWN_SESSIONS]:
                removed = kept.pop(sid, None)
                if removed is not None:
                    pruned_until = max(pruned_until, float(removed["ended_at"]))
            return kept, pruned_until
        return kept, 0.0


def collector_from_env(send: Callable[[list[dict[str, Any]]], bool]) -> DevinCollector:
    """環境変数から収集器を組み立てる。資格情報は環境変数からだけ読む。"""
    api_key = os.environ.get("DEVIN_API_KEY", "").strip()
    org_id = os.environ.get("DEVIN_ORG_ID", "").strip()
    if not api_key or not org_id:
        raise ValueError("DEVIN_API_KEY and DEVIN_ORG_ID are required")
    base = (
        os.environ.get("DEVIN_API_BASE", DEFAULT_API_BASE).strip() or DEFAULT_API_BASE
    )
    state_path = os.environ.get("SENDA_ARGUS_DEVIN_STATE", "").strip() or os.path.join(
        os.path.expanduser("~"),
        ".senda_argus",
        f"devin_{hashlib.sha256(org_id.encode()).hexdigest()[:12]}.json",
    )

    def _flag(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

    return DevinCollector(
        DevinApi(api_key=api_key, org_id=org_id, base_url=base),
        org_id=org_id,
        send=send,
        state_path=state_path,
        agent_id=os.environ.get("SENDA_ARGUS_DEVIN_AGENT_ID", ""),
        project=os.environ.get("SENDA_ARGUS_PROJECT", "devin") or "devin",
        environment=os.environ.get("SENDA_ARGUS_ENVIRONMENT", "cloud") or "cloud",
        capture_prompt=_flag("SENDA_ARGUS_CAPTURE_PROMPT"),
        redact=os.environ.get("SENDA_ARGUS_REDACT", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        include_audit_logs=_flag("SENDA_ARGUS_DEVIN_AUDIT_LOGS"),
    )
