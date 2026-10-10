"""Argus が判定できない時の扱い。

Argus が新しい判定を行えない理由を符号で表し、符号ごとに 止める、保留する、記録して通す の
いずれかを選ぶ。エージェントの行動を実行する場所は、実行の前に ``before_action`` を呼ぶ。
判定を省いたときは、黙って許可せずに警告のログを出し、判定を省いた記録を送出のキューへ積む。

符号:
    actions_exhausted : 判定に使う Actions の不足
    quota_unknown     : 残量の照会先へ届かず、残量が分からない
    read_only         : 組織が閲覧専用
    argus_unavailable : Argus に届かない、または Argus が応答しない

扱い:
    block : 行動を実行せず ``UnjudgedActionBlocked`` を投げる
    hold  : 判定ができるようになるまで照会を繰り返して待つ。上限に達したら止める
    pass  : 警告を出し、判定を省いた印を記録に載せて実行する

方針は受け取り側が判定の可否の照会で返すものを優先する。受け取れないときは最後に受け取った
方針を使い、それも無ければ手元の設定 ``SENDA_ARGUS_UNJUDGED_POLICY`` を使う。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .hashing import sha256_value

_logger = logging.getLogger("senda_argus_hooks.unjudged")

ACTIONS_EXHAUSTED = "actions_exhausted"
QUOTA_UNKNOWN = "quota_unknown"
READ_ONLY = "read_only"
ARGUS_UNAVAILABLE = "argus_unavailable"
REASONS = (ACTIONS_EXHAUSTED, QUOTA_UNKNOWN, READ_ONLY, ARGUS_UNAVAILABLE)

BLOCK = "block"
HOLD = "hold"
PASS = "pass"
ACTIONS = (BLOCK, HOLD, PASS)

DEFAULT_POLICY: dict[str, str] = {
    ACTIONS_EXHAUSTED: BLOCK,
    QUOTA_UNKNOWN: HOLD,
    READ_ONLY: BLOCK,
    ARGUS_UNAVAILABLE: PASS,
}

ENV_POLICY = "SENDA_ARGUS_UNJUDGED_POLICY"
ENV_HOLD_SECONDS = "SENDA_ARGUS_UNJUDGED_HOLD_SECONDS"
ENV_GUARD = "SENDA_ARGUS_UNJUDGED_GUARD"
DEFAULT_HOLD_SECONDS = 30.0

# 判定を省いた記録の種別。本文は理由の符号、扱い、時刻、ツールの名前のダイジェストだけにする。
UNJUDGED_EVENT_TYPE = "argus.unjudged"

ADMISSION_PATH = "/v1/usage/admission"
# 控えの秒数は受け取り側の値を 1 以上 300 以下に丸める。0 や負の値で照会を連打せず、
# 大きすぎる値で状態の変化を見落とさない。
TTL_MIN = 1.0
TTL_MAX = 300.0
DEFAULT_TTL = 60.0
# 照会に失敗したときの控えの秒数。届かない間に行動ごとに接続の待ちを重ねない。
FAILURE_TTL = 10.0
DEFAULT_ADMISSION_TIMEOUT = 3.0
DEFAULT_HOLD_POLL = 2.0

# 取り込みの拒否の error_code から理由の符号を引く。閲覧専用だけ符号の名前が違う。
ERROR_CODE_REASONS: dict[str, str] = {
    "actions_exhausted": ACTIONS_EXHAUSTED,
    "quota_unknown": QUOTA_UNKNOWN,
    "organization_read_only": READ_ONLY,
}


class UnjudgedActionBlocked(RuntimeError):
    """Argus が判定できないため、行動を実行しなかったことを示す。"""

    def __init__(self, reason_code: str, action: str, tool: str | None = None) -> None:
        self.reason_code = reason_code
        self.action = action
        self.tool = tool
        super().__init__(
            f"Argus が判定できないため行動を止めました reason_code={reason_code} action={action}"
        )


def _valid_pair(reason: Any, action: Any) -> bool:
    return isinstance(reason, str) and reason in REASONS and isinstance(action, str) and action in ACTIONS


def parse_policy(text: str | None) -> dict[str, str]:
    """``reason=action,reason=action`` を読み、既定の方針へ重ねた方針を返す。

    知らない符号と扱いの項目は警告して捨てる。他の項目は活かす。
    """
    policy = dict(DEFAULT_POLICY)
    if not text:
        return policy
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        reason, sep, action = item.partition("=")
        reason = reason.strip().lower()
        action = action.strip().lower()
        if not sep or not _valid_pair(reason, action):
            _logger.warning("%s の項目を無視しました。知らない符号か扱いです: %s", ENV_POLICY, item)
            continue
        policy[reason] = action
    return policy


def policy_from_server(value: Any) -> dict[str, str] | None:
    """受け取り側の unjudged_policy を検査する。正しい項目が 1 つも無ければ None を返す。

    受け取り側が一部の符号を返さないときは、その符号を既定の方針で補う。
    """
    if not isinstance(value, Mapping):
        return None
    policy = dict(DEFAULT_POLICY)
    valid = 0
    for reason, action in value.items():
        if _valid_pair(reason, action):
            policy[reason] = action
            valid += 1
        else:
            _logger.warning("受け取り側の方針の項目を無視しました。知らない符号か扱いです: %s=%s", reason, action)
    return policy if valid else None


def hold_seconds_from_env() -> float:
    raw = os.getenv(ENV_HOLD_SECONDS)
    if raw is None or not raw.strip():
        return DEFAULT_HOLD_SECONDS
    try:
        value = float(raw)
    except ValueError:
        _logger.warning("%s を読めないため既定の %s 秒を使います: %s", ENV_HOLD_SECONDS, DEFAULT_HOLD_SECONDS, raw)
        return DEFAULT_HOLD_SECONDS
    return max(0.0, value)


def guard_disabled_by_env() -> bool:
    return (os.getenv(ENV_GUARD) or "").strip().lower() in {"off", "0", "false", "no"}


def clamp_ttl(value: Any) -> float:
    try:
        ttl = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TTL
    if ttl != ttl:  # NaN
        return DEFAULT_TTL
    return min(TTL_MAX, max(TTL_MIN, ttl))


@dataclass(frozen=True)
class Admission:
    """判定の可否の照会の結果。admitted が偽なら reason_code と action を持つ。"""

    admitted: bool
    reason_code: str | None = None
    action: str | None = None


ADMITTED = Admission(True)


@dataclass(frozen=True)
class Unjudged:
    """判定を省いて通した行動の印。記録へ載せる。"""

    reason_code: str
    action: str

    def as_fields(self) -> dict[str, Any]:
        return {"unjudged": True, "unjudged_reason": self.reason_code, "unjudged_action": self.action}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AdmissionGuard:
    """行動の前に Argus の判定の可否を照会し、方針に従って止める、待つ、通す。"""

    def __init__(
        self,
        endpoint: str,
        api_key: str = "",
        *,
        local_policy: Mapping[str, str] | None = None,
        hold_seconds: float | None = None,
        timeout: float = DEFAULT_ADMISSION_TIMEOUT,
        hold_poll: float = DEFAULT_HOLD_POLL,
        emit: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = endpoint.rstrip("/") + ADMISSION_PATH
        self._api_key = api_key
        self._local_policy = dict(local_policy) if local_policy is not None else parse_policy(os.getenv(ENV_POLICY))
        self._hold_seconds = hold_seconds_from_env() if hold_seconds is None else max(0.0, float(hold_seconds))
        self._timeout = timeout
        self._hold_poll = max(0.01, float(hold_poll))
        self._emit = emit
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._cached: Admission | None = None
        self._cached_until = 0.0
        self._server_policy: dict[str, str] | None = None
        self._legacy_logged = False

    # 方針 -------------------------------------------------------------------

    def policy(self) -> dict[str, str]:
        """今の方針。受け取り側の方針を手元の設定より優先する。"""
        with self._lock:
            server = self._server_policy
        return dict(server) if server is not None else dict(self._local_policy)

    def _action_for(self, reason: str) -> str:
        return self.policy().get(reason, DEFAULT_POLICY[reason])

    # 照会 -------------------------------------------------------------------

    def admission(self, *, refresh: bool = False) -> Admission:
        """判定の可否を返す。控えの期限の内は照会しない。"""
        now = self._clock()
        if not refresh:
            with self._lock:
                if self._cached is not None and now < self._cached_until:
                    return self._cached
        result, ttl = self._query()
        with self._lock:
            self._cached = result
            self._cached_until = self._clock() + ttl
        return result

    def _unavailable(self, ttl: float = FAILURE_TTL) -> tuple[Admission, float]:
        return Admission(False, ARGUS_UNAVAILABLE, self._action_for(ARGUS_UNAVAILABLE)), ttl

    def _query(self) -> tuple[Admission, float]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        req = urllib.request.Request(self._url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:  # noqa: S310 - 送出器と同じ設定の宛先
                raw = response.read(65536)
        except urllib.error.HTTPError as exc:
            return self._from_http_error(exc)
        except (urllib.error.URLError, OSError, ValueError):
            # 接続の失敗とタイムアウトは Argus に届かない状態として扱う。
            return self._unavailable()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return self._unavailable()
        if not isinstance(body, dict) or not isinstance(body.get("admitted"), bool):
            return self._unavailable()
        server_policy = policy_from_server(body.get("unjudged_policy"))
        if server_policy is not None:
            with self._lock:
                self._server_policy = server_policy
        ttl = clamp_ttl(body.get("ttl_seconds", DEFAULT_TTL))
        if body["admitted"]:
            return ADMITTED, ttl
        reason = body.get("reason_code")
        if not isinstance(reason, str) or reason not in REASONS:
            # 判定できないと返したのに符号が分からないときは、Argus の障害と同じに扱う。
            reason = ARGUS_UNAVAILABLE
        action = body.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            action = self._action_for(reason)
        return Admission(False, reason, action), ttl

    def _from_http_error(self, exc: urllib.error.HTTPError) -> tuple[Admission, float]:
        if exc.code == 404:
            # 判定の可否の照会を持たない古い受け取り側。照会の無い頃と同じく許可として扱う。
            if not self._legacy_logged:
                self._legacy_logged = True
                _logger.info("Argus が判定の可否の照会を持たないため、行動の前の確認を省きます: %s", self._url)
            return ADMITTED, DEFAULT_TTL
        if exc.code in (401, 403):
            # 拒否の符号が判定できない理由を表すなら、その理由として扱う。それ以外の 401 と 403、
            # つまり鍵の誤りや組織の停止は、取り込みでも送り直さず記録が届かない状態であり、
            # Argus に届かないときと同じ扱いにする。組織の停止で行動を止めない既存の扱いと揃い、
            # 黙って通さず警告と記録を残す。止めたい利用者は argus_unavailable=block を選ぶ。
            code = None
            try:
                body = json.loads(exc.read(65536).decode("utf-8"))
                code = body.get("error_code") if isinstance(body, dict) else None
            except Exception:  # noqa: BLE001
                code = None
            reason = ERROR_CODE_REASONS.get(code) if isinstance(code, str) else None
            if reason is not None:
                return Admission(False, reason, self._action_for(reason)), FAILURE_TTL
            return self._unavailable()
        # 5xx とその他の失敗は Argus の障害として扱う。
        return self._unavailable()

    # 行動の前の確認 -----------------------------------------------------------

    def before_action(self, tool: str | None = None) -> Unjudged | None:
        """行動の前に呼ぶ。通すなら None か印を返し、止めるなら例外を投げる。"""
        result = self.admission()
        if result.admitted:
            return None
        if result.action == HOLD:
            result = self._hold_sync(result)
            if result.admitted:
                return None
        return self._decide(result, tool)

    async def before_action_async(self, tool: str | None = None) -> Unjudged | None:
        """``before_action`` の非同期版。照会と待ちでイベントループを塞がない。"""
        result = await asyncio.to_thread(self.admission)
        if result.admitted:
            return None
        if result.action == HOLD:
            deadline = self._clock() + self._hold_seconds
            while result.action == HOLD and not result.admitted:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(self._hold_poll, remaining))
                result = await asyncio.to_thread(self.admission, refresh=True)
            if result.admitted:
                return None
        return self._decide(result, tool)

    def _hold_sync(self, result: Admission) -> Admission:
        deadline = self._clock() + self._hold_seconds
        while result.action == HOLD and not result.admitted:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            self._sleep(min(self._hold_poll, remaining))
            result = self.admission(refresh=True)
        return result

    def _decide(self, result: Admission, tool: str | None) -> Unjudged | None:
        reason = result.reason_code or ARGUS_UNAVAILABLE
        action = result.action or self._action_for(reason)
        self._record(reason, action, tool)
        if action == PASS:
            _logger.warning(
                "Argus が判定できないため、判定を省いて行動を実行します reason_code=%s", reason
            )
            return Unjudged(reason, action)
        if action == HOLD:
            _logger.warning(
                "Argus が %.0f 秒の内に判定できるようにならなかったため、行動を止めました reason_code=%s",
                self._hold_seconds,
                reason,
            )
        else:
            _logger.warning("Argus が判定できないため、行動を止めました reason_code=%s", reason)
        raise UnjudgedActionBlocked(reason, action, tool)

    def _record(self, reason: str, action: str, tool: str | None) -> None:
        emit = self._emit
        if emit is None:
            from .runtime import emit_event as emit
        data = {
            "unjudged": {
                "reason_code": reason,
                "action": action,
                "at": _now_iso(),
                "tool_name_hash": sha256_value(str(tool)) if tool is not None else None,
            }
        }
        try:
            emit(
                UNJUDGED_EVENT_TYPE,
                data=data,
                source={"component": "unjudged_guard", "sdk": "senda_argus_hooks"},
                status="success",
            )
        except Exception:  # noqa: BLE001
            _logger.warning("判定を省いた記録を積めませんでした reason_code=%s", reason, exc_info=True)


# 処理の全体で 1 つの確認を持つ。Argus の送出器が設定された処理でだけ有効にする。
_guard: AdmissionGuard | None = None
_guard_lock = threading.Lock()
_disabled_warned = False


def get_guard() -> AdmissionGuard | None:
    return _guard


def set_guard(guard: AdmissionGuard | None) -> None:
    global _guard
    with _guard_lock:
        _guard = guard


def configure_guard(endpoint: str | None, api_key: str = "", **kwargs: Any) -> AdmissionGuard | None:
    """送出器の宛先で確認を有効にする。宛先が無ければ無効にする。

    ``SENDA_ARGUS_UNJUDGED_GUARD=off`` のときは無効にし、起動ごとに 1 回だけ警告する。
    """
    global _disabled_warned
    if not endpoint:
        set_guard(None)
        return None
    if guard_disabled_by_env():
        set_guard(None)
        if not _disabled_warned:
            _disabled_warned = True
            _logger.warning(
                "%s=off のため、Argus が判定できない時も行動を止めずに実行します", ENV_GUARD
            )
        return None
    guard = AdmissionGuard(endpoint, api_key, **kwargs)
    set_guard(guard)
    return guard


def check_before_action(tool: str | None = None) -> Unjudged | None:
    """確認が有効なら行動の前に照会する。無効なら何もしない。"""
    guard = _guard
    if guard is None:
        return None
    return guard.before_action(tool)


async def check_before_action_async(tool: str | None = None) -> Unjudged | None:
    guard = _guard
    if guard is None:
        return None
    return await guard.before_action_async(tool)
