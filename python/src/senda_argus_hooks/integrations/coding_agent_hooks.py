"""コーディングエージェントの hook と Argus の実行の前の判定をつなぐ共通の部分。

PRODUCTS の各製品の hook から呼ばれ、製品の入力を読み、POST /v1/hooks/precheck の判定を製品の
応答の形へ写す。製品ごとの違いは入力の読み方と応答の書き方だけで、判定の待ち方、時間の上限、
失敗の扱いはここに 1 つだけ置く。

失敗の扱い:
- 内部の上限に達したら、製品の時間切れを待たずにアダプタ自身が deny を返す。製品の側の時間切れは、
  許可に倒れると公式に書かれた製品と、扱いが公式に書かれていない製品があるため
- 接続できない、503 などの判定できない応答、形の不正な応答は、組織の設定 fail_open が true の
  ときだけ allow にする。既定は deny
- 400、401、403 はサーバーが要求を拒んだ応答で、判定できない状態ではない。fail_open に
  関わらず deny にする。鍵を失効させても fail_open で素通りになる経路を作らない
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

PRODUCTS = (
    "claude_code",
    "codex",
    "copilot_cli",
    "cursor",
    "vscode_copilot",
    "windsurf",
    "gemini_cli",
    "qwen_code",
    "kiro",
    "amazon_q_cli",
    "factory_droid",
    "crush",
    "goose",
    "augment_code",
    "junie_cli",
    "cline",
    "antigravity_cli",
)
DECISIONS = frozenset({"allow", "deny", "pending"})

PRECHECK_PATH = "/v1/hooks/precheck"
POSTCALL_PATH = "/v1/hooks/postcall"

# 管理の導入で読む設定ファイル。root の所有で、グループと他者に書き込みの無いときだけ信頼する。
MANAGED_CONFIG_PATH = Path("/etc/senda-argus/hooks.env")

# 導入のコマンドが各製品の設定へ書く hook の時間の上限 (秒)。Copilot CLI の既定と同じ 30 秒にする。
PRODUCT_HOOK_TIMEOUT_SEC = 30
# 製品の時間の上限から引く余裕。Python の起動と応答の書き出しの分。
_PRODUCT_TIMEOUT_MARGIN_SEC = 8
# 製品の時間の上限。導入のコマンドが時間を書く製品はその値、書けない製品は公式の既定のうち最も短い
# 30 秒 (Amazon Q と Cline と VS Code)。Windsurf と Cursor は文書に秒数が無いため、同じ 30 秒を上限とみなす。
PRODUCT_TIMEOUT_SEC: dict[str, int] = dict.fromkeys(PRODUCTS, PRODUCT_HOOK_TIMEOUT_SEC)
# 承認を待つ全体の上限の既定と、1 回の要求の上限の既定。
DEFAULT_WAIT_LIMIT_SEC = 20.0
DEFAULT_REQUEST_TIMEOUT_SEC = 10.0
POSTCALL_TIMEOUT_SEC = 5.0
POLL_INTERVAL_SEC = 1.0
# サーバーが受理する本文の上限は 1 MiB。これを超える本文は 413 で拒まれる。拒まれた呼び出しを
# fail_open で素通りさせないため、送る前に長さを見て、超えるものは遮断にする。
REQUEST_BODY_MAX_BYTES = 1_000_000
# 実行の後の記録を、サーバー側の一時的な失敗で落とさないための再送の回数と間隔。
POSTCALL_RETRIES = 1
POSTCALL_RETRY_DELAY_SEC = 0.5
# 実行の後の記録で送る tool_response の文字数の上限。
TOOL_RESPONSE_MAX_CHARS = 4096
# 応答の本文を読む上限。
_RESPONSE_READ_LIMIT = 65536

_CONFIG_KEYS = (
    "SENDA_ARGUS_ENDPOINT",
    "SENDA_ARGUS_API_KEY",
    "SENDA_ARGUS_AGENT_ID",
    "SENDA_ARGUS_HOOK_FAIL_OPEN",
    "SENDA_ARGUS_HOOK_WAIT_LIMIT_SEC",
    "SENDA_ARGUS_HOOK_REQUEST_TIMEOUT_SEC",
)


class HookConfigError(Exception):
    """設定を信頼して読めない。判別できないため遮断に倒す。"""


class InputError(Exception):
    """製品の入力を読めない。"""


@dataclass
class HookConfig:
    endpoint: str
    api_key: str = ""
    agent_id: str = ""
    fail_open: bool = False
    wait_limit_sec: float = DEFAULT_WAIT_LIMIT_SEC
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC


@dataclass
class ToolCall:
    """製品の入力を、約束の要求の項目へ読み替えた 1 件。"""

    product: str
    session_id: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str | None = None
    cwd: str | None = None
    user_email: str | None = None
    tool_response: Any = None
    has_response: bool = False


@dataclass
class Verdict:
    """製品へ返す結論。allow か deny の 2 つだけ。"""

    allow: bool
    reason: str = ""
    request_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in _CONFIG_KEYS:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _check_managed_owner(st: os.stat_result) -> None:
    """管理の設定ファイルが root の所有で、グループと他者に書き込みが無いことを確かめる。

    利用者が書けるファイルを信頼すると、エージェントが fail_open や送信先を書き換えて、
    通信を断つだけで判定を外せる。
    """
    if st.st_uid != 0:
        raise HookConfigError("管理の設定ファイルが root の所有でない")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise HookConfigError(
            "管理の設定ファイルにグループか他者の書き込みの権限がある"
        )


# 試験で所有者の検査を差し替えるための参照。
managed_owner_check: Callable[[os.stat_result], None] = _check_managed_owner


def _to_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _to_positive_float(value: str | None, default: float) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if number != number or number <= 0 or number == float("inf"):
        return default
    return number


def load_hook_config(product: str, config_path: str | None) -> HookConfig:
    """hook の設定を読む。

    config_path を渡したときは管理の導入で、そのファイルだけを読む。環境変数と利用者の階層の
    設定ファイルは読まない。どちらも利用者とエージェントが書き換えられるため。
    渡さないときは試用の導入で、既存の設定ファイルと環境変数から読む。
    """
    if config_path:
        path = Path(config_path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                managed_owner_check(os.fstat(handle.fileno()))
                values = _parse_env_text(handle.read(_RESPONSE_READ_LIMIT))
        except OSError as exc:
            raise HookConfigError(
                f"管理の設定ファイルを読めない: {exc.__class__.__name__}"
            ) from exc
    else:
        from senda_argus_hooks.config import load_config_files

        load_config_files()
        values = {key: os.environ[key] for key in _CONFIG_KEYS if key in os.environ}
    endpoint = values.get("SENDA_ARGUS_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        raise HookConfigError("SENDA_ARGUS_ENDPOINT が無い")
    product_cap = float(PRODUCT_TIMEOUT_SEC[product] - _PRODUCT_TIMEOUT_MARGIN_SEC)
    wait_limit = min(
        _to_positive_float(
            values.get("SENDA_ARGUS_HOOK_WAIT_LIMIT_SEC"), DEFAULT_WAIT_LIMIT_SEC
        ),
        product_cap,
    )
    request_timeout = min(
        _to_positive_float(
            values.get("SENDA_ARGUS_HOOK_REQUEST_TIMEOUT_SEC"),
            DEFAULT_REQUEST_TIMEOUT_SEC,
        ),
        wait_limit,
    )
    agent_id = (
        values.get("SENDA_ARGUS_AGENT_ID", "").strip()
        or f"{product}@{socket.gethostname()}"
    )
    return HookConfig(
        endpoint=endpoint,
        api_key=values.get("SENDA_ARGUS_API_KEY", "").strip(),
        agent_id=agent_id,
        fail_open=_to_bool(values.get("SENDA_ARGUS_HOOK_FAIL_OPEN")),
        wait_limit_sec=wait_limit,
        request_timeout_sec=request_timeout,
    )


class TransportError(Exception):
    """判定できない失敗。接続できない、503 など、応答が不正。fail_open の対象。"""


class RejectedError(Exception):
    """サーバーが要求を拒んだ。400、401、403。fail_open の対象にしない。"""


def _post_json(
    url: str, body: dict[str, Any], api_key: str, timeout: float
) -> tuple[int, Any]:
    payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
    if len(payload) > REQUEST_BODY_MAX_BYTES:
        raise RejectedError(f"要求の本文が {REQUEST_BODY_MAX_BYTES} バイトを超える")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=max(0.1, timeout)) as response:
            raw = response.read(_RESPONSE_READ_LIMIT)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except Exception as exc:  # noqa: BLE001 - 接続の失敗は種類を問わず判定できない状態として扱う
        raise TransportError(exc.__class__.__name__) from exc
    try:
        return status, json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TransportError("応答の本文を読めない") from exc


# 試験で HTTP を差し替えるための参照。
post_json: Callable[[str, dict[str, Any], str, float], tuple[int, Any]] = _post_json


def build_request_body(call: ToolCall, config: HookConfig) -> dict[str, Any]:
    body: dict[str, Any] = {
        "product": call.product,
        "agent_id": config.agent_id,
        "session_id": call.session_id,
        "tool_name": call.tool_name,
        "tool_input": call.tool_input,
    }
    if call.tool_use_id:
        body["tool_use_id"] = call.tool_use_id
    if call.cwd:
        body["cwd"] = call.cwd
    if call.user_email:
        body["user_email"] = call.user_email
    return body


def _validate_precheck(body: Any) -> dict[str, Any]:
    """約束の応答の形を確かめる。外れたら判定できない状態として扱う。"""
    if not isinstance(body, dict):
        raise TransportError("応答が JSON のオブジェクトでない")
    decision = body.get("decision")
    if decision not in DECISIONS:
        raise TransportError("decision が不正")
    wait_hint = body.get("wait_hint_sec", 0)
    if isinstance(wait_hint, bool) or not isinstance(wait_hint, int) or wait_hint < 0:
        raise TransportError("wait_hint_sec が不正")
    request_id = body.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        raise TransportError("request_id が不正")
    if decision == "pending" and not request_id:
        raise TransportError("pending に request_id が無い")
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise TransportError("reason が不正")
    return body


def call_with_deadline(fn: Callable[[], Any], timeout: float) -> Any:
    """fn を別のスレッドで動かし、timeout 秒で打ち切る。

    urllib の timeout は接続と受信の 1 回ごとに効き、名前の解決と少しずつ届く応答では要求の全体を
    区切らない。全体を区切らないと製品の時間切れまで進み、製品の側で許可に倒れる。
    打ち切ったスレッドは daemon で、hook のプロセスの終了で消える。
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 呼び出し側へそのまま渡す
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(max(0.0, timeout))
    if worker.is_alive():
        raise TransportError("判定の応答が時間の上限までに届かない")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _precheck_once(
    call: ToolCall, config: HookConfig, timeout: float
) -> dict[str, Any]:
    url = config.endpoint + PRECHECK_PATH
    request_body = build_request_body(call, config)
    status, body = call_with_deadline(
        lambda: post_json(url, request_body, config.api_key, timeout), timeout
    )
    # 413 は本文が大きすぎ、429 は呼び出しの多さで拒まれた。判定できない状態ではなく、拒まれた応答。
    # fail_open で許可にすると、大きな本文や大量の呼び出しで判定を避けられる。
    if status in (400, 401, 403, 413, 429):
        raise RejectedError(f"判定の要求が {status} で拒まれた")
    if status != 200:
        raise TransportError(f"判定の応答が {status}")
    return _validate_precheck(body)


def _pending_reason(body: dict[str, Any]) -> str:
    request_id = body.get("request_id") or ""
    reason = body.get("reason") or "Argus の承認待ち"
    if request_id and request_id not in reason:
        reason = f"{reason} 承認の依頼の番号: {request_id}"
    return reason


def _verdict_from(body: dict[str, Any]) -> Verdict:
    decision = body["decision"]
    if decision == "allow":
        return Verdict(allow=True, reason=body.get("reason") or "", detail=body)
    if decision == "pending":
        return Verdict(
            allow=False,
            reason=_pending_reason(body),
            request_id=body.get("request_id"),
            detail=body,
        )
    return Verdict(
        allow=False, reason=body.get("reason") or "Argus が実行を止めた", detail=body
    )


def decide(
    call: ToolCall,
    config: HookConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Verdict:
    """実行の前の判定を取り、製品へ返す結論にする。

    pending で wait_hint_sec が正のときだけ、同じ要求を 1 秒ごとに再送して allow を待つ。待つのは
    wait_hint_sec と内部の上限の短い方までで、上限に達したら deny を返す。
    """
    start = clock()
    deadline = start + config.wait_limit_sec
    try:
        body = _precheck_once(
            call, config, min(config.request_timeout_sec, config.wait_limit_sec)
        )
    except RejectedError as exc:
        return Verdict(
            allow=False, reason=f"Argus が判定の要求を拒んだため実行を止めた: {exc}"
        )
    except TransportError as exc:
        if config.fail_open:
            return Verdict(
                allow=True, reason=f"Argus で判定できず組織の設定により許可した: {exc}"
            )
        return Verdict(
            allow=False, reason=f"Argus で判定できないため実行を止めた: {exc}"
        )
    if clock() >= deadline:
        return Verdict(
            allow=False, reason="Argus の判定が内部の上限の時間を超えたため実行を止めた"
        )
    verdict = _verdict_from(body)
    if verdict.allow or body["decision"] != "pending" or body["wait_hint_sec"] <= 0:
        return verdict
    wait_until = min(deadline, start + float(body["wait_hint_sec"]))
    while True:
        now = clock()
        if now + POLL_INTERVAL_SEC > wait_until:
            break
        sleep(POLL_INTERVAL_SEC)
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            polled = _precheck_once(
                call, config, min(config.request_timeout_sec, remaining)
            )
        except (RejectedError, TransportError):
            # 承認待ちの途中の失敗は、fail_open で許可へ変えない。既に受けた承認待ちのまま止める。
            break
        if clock() > deadline:
            break
        if polled["decision"] == "allow":
            return _verdict_from(polled)
        if polled["decision"] == "deny":
            return _verdict_from(polled)
        verdict = _verdict_from(polled)
    if clock() >= deadline:
        verdict.reason = f"{verdict.reason} 内部の上限の時間に達したため実行を止めた"
    return verdict


def truncate_tool_response(value: Any) -> Any:
    """tool_response を 4096 文字に切る。超えたものは JSON の文字列にして先頭を送る。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:TOOL_RESPONSE_MAX_CHARS]
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    if len(text) <= TOOL_RESPONSE_MAX_CHARS:
        return value
    return text[:TOOL_RESPONSE_MAX_CHARS]


def send_postcall(call: ToolCall, config: HookConfig) -> bool:
    """実行の後の記録を送る。失敗しても例外を出さない。製品の実行を止めないため。

    サーバー側の一時的な失敗 (5xx と接続の失敗) は、短い間隔で再送する。再送が無いと、記録が届かない
    呼び出しが静かな欠落として警報になる。
    """
    for attempt in range(POSTCALL_RETRIES + 1):
        try:
            body = build_request_body(call, config)
            body["tool_response"] = truncate_tool_response(call.tool_response)
            status, _ = post_json(
                config.endpoint + POSTCALL_PATH,
                body,
                config.api_key,
                POSTCALL_TIMEOUT_SEC,
            )
        except RejectedError:
            return False
        except Exception:  # noqa: BLE001 - 記録の失敗で製品の実行を止めない
            status = 0
        if status == 200:
            return True
        if status and status < 500:
            return False
        if attempt < POSTCALL_RETRIES:
            time.sleep(POSTCALL_RETRY_DELAY_SEC)
    return False


def read_stdin_json(stream: TextIO) -> dict[str, Any]:
    try:
        data = json.loads(stream.read())
    except Exception as exc:  # noqa: BLE001
        raise InputError("hook の入力が JSON でない") from exc
    if not isinstance(data, dict):
        raise InputError("hook の入力が JSON のオブジェクトでない")
    return data


def coerce_tool_input(value: Any) -> dict[str, Any]:
    """tool_input をオブジェクトにする。JSON の文字列なら読む。オブジェクトにならなければ入力の誤り。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception as exc:  # noqa: BLE001
            raise InputError("tool の引数を読めない") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InputError("tool の引数がオブジェクトでない")
    return value


def optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def required_str(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    raise InputError(f"{keys[0]} が無い")


@dataclass
class HookOutput:
    stdout: str
    stderr: str
    exit_code: int


class ProductAdapter:
    """製品ごとの入力の読み方と応答の書き方。"""

    product: str = ""

    def parse_pre(self, data: dict[str, Any]) -> ToolCall:
        raise NotImplementedError

    def parse_post(self, data: dict[str, Any]) -> ToolCall:
        raise NotImplementedError

    # 遮断の終了コード。終了コード 2 を遮断と公式に書く製品は 2。標準出力の JSON だけが公式の遮断の
    # 返し方で、終了コードの意味が書かれていない製品は 0 にする。0 以外を hook の失敗として標準出力を
    # 捨てる製品で、遮断の JSON を読ませずに許可へ倒さないため。
    deny_exit_code: int = 2

    def render_deny(self, reason: str) -> dict[str, Any] | None:
        """遮断のときの標準出力。None は何も書かず、終了コードと標準エラー出力だけで遮断を返す。"""
        raise NotImplementedError

    def render_allow(self) -> dict[str, Any] | None:
        """許可のときの出力。None は何も書かず、製品の通常の許可の流れへ進める。"""
        return None

    def render_post(self) -> dict[str, Any] | None:
        """実行の後の hook の標準出力。応答を必ず返すと公式に書かれた製品だけが書く。"""
        return None

    def deny_output(self, reason: str) -> HookOutput:
        # 遮断は製品の deny の JSON と終了コードで返す。終了コード 2 では stderr を理由として読む製品がある。
        rendered = self.render_deny(reason)
        return HookOutput(
            stdout="" if rendered is None else json.dumps(rendered, ensure_ascii=False),
            stderr=reason,
            exit_code=self.deny_exit_code,
        )

    def allow_output(self) -> HookOutput:
        rendered = self.render_allow()
        return HookOutput(
            stdout="" if rendered is None else json.dumps(rendered, ensure_ascii=False),
            stderr="",
            exit_code=0,
        )


def run_pre(
    adapter: ProductAdapter, stream: TextIO, config_path: str | None
) -> HookOutput:
    """実行の前の hook の 1 回。どの失敗でも deny か、fail_open の許可のどちらかを返す。"""
    try:
        call = adapter.parse_pre(read_stdin_json(stream))
    except InputError as exc:
        return adapter.deny_output(
            f"Argus が hook の入力を読めないため実行を止めた: {exc}"
        )
    try:
        config = load_hook_config(adapter.product, config_path)
    except HookConfigError as exc:
        # 設定を信頼して読めないときは fail_open も読めないため、遮断に倒す。
        return adapter.deny_output(
            f"Argus の hook の設定を読めないため実行を止めた: {exc}"
        )
    try:
        verdict = decide(call, config)
    except Exception as exc:  # noqa: BLE001 - 想定外の失敗も製品の時間切れに任せず遮断を返す
        return adapter.deny_output(
            f"Argus の hook が失敗したため実行を止めた: {exc.__class__.__name__}"
        )
    if verdict.allow:
        return adapter.allow_output()
    return adapter.deny_output(verdict.reason)


def run_post(
    adapter: ProductAdapter, stream: TextIO, config_path: str | None
) -> HookOutput:
    """実行の後の hook の 1 回。結果に関わらず終了コード 0 を返し、製品の実行を止めない。"""
    try:
        call = adapter.parse_post(read_stdin_json(stream))
        config = load_hook_config(adapter.product, config_path)
        send_postcall(call, config)
    except Exception:  # noqa: BLE001, S110 - 記録の失敗で製品の実行を止めない
        pass
    rendered = adapter.render_post()
    return HookOutput(
        stdout="" if rendered is None else json.dumps(rendered, ensure_ascii=False),
        stderr="",
        exit_code=0,
    )


def adapter_for(product: str) -> ProductAdapter:
    from .hook_products import ADAPTERS

    factory = ADAPTERS.get(product)
    if factory is None:
        raise ValueError(f"未対応の製品: {product}")
    return factory()


def main(
    product: str,
    phase: str,
    config_path: str | None = None,
    stream: TextIO | None = None,
) -> int:
    """hook の入口。製品の応答を標準出力へ書き、終了コードを返す。"""
    adapter = adapter_for(product)
    source = stream if stream is not None else sys.stdin
    if phase == "pre":
        output = run_pre(adapter, source, config_path)
    elif phase == "post":
        output = run_post(adapter, source, config_path)
    else:
        raise ValueError(f"未対応の段階: {phase}")
    if output.stdout:
        sys.stdout.write(output.stdout + "\n")
        sys.stdout.flush()
    if output.stderr:
        sys.stderr.write(output.stderr + "\n")
        sys.stderr.flush()
    return output.exit_code
