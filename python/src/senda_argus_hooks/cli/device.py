"""端末の登録と状態の確認と鍵の更新。

登録は、管理者が発行した使い切りの登録コードと、利用者が Argus の画面で行う SSO の承認の両方を要する。
組織の API キーだけでは端末を登録できない。人の識別は画面の SSO のセッションで確定し、この CLI は
OS の利用者名を自己申告として送るだけである。

    senda-hooks device register --endpoint https://argus.example --code <登録コード>
    senda-hooks device status
    senda-hooks device renew

鍵は core/device_identity.py の保存先へ、利用者だけが読める権限で書く。画面と標準出力に秘密は出さない。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from senda_argus_hooks.core.device_keys import (
    KeyProvider,
    challenge_for,
    select_provider,
)
from senda_argus_hooks.core.device_identity import (
    DeviceCredential,
    hostname,
    load_credential,
    os_user,
    default_path,
    save_credential,
    sign_headers,
)

ENV_ENDPOINT = "SENDA_ARGUS_ENDPOINT"
ENV_CODE = "SENDA_ARGUS_REGISTRATION_CODE"
ENV_DASHBOARD = "SENDA_ARGUS_DASHBOARD_URL"
_NETWORK_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    http.client.HTTPException,
    OSError,
)
_RESPONSE_LIMIT = 65_536
_MIN_INTERVAL = 1.0
_MAX_INTERVAL = 30.0


def _epoch(value: Any) -> Optional[float]:
    """応答の期限は、エポック秒と ISO 8601 のどちらでも読む。読めない値は期限なしにせず None を返す。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _post(
    url: str, body: dict[str, Any], *, timeout: float, signed: bool = False
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if signed:
        headers.update(sign_headers(data))
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read(_RESPONSE_LIMIT)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read(_RESPONSE_LIMIT) if exc.fp is not None else b""
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _endpoint(args: argparse.Namespace, credential: Optional[DeviceCredential]) -> str:
    value = (
        getattr(args, "endpoint", None)
        or os.environ.get(ENV_ENDPOINT, "")
        or (credential.endpoint if credential else "")
    )
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError(
            f"--endpoint か環境変数 {ENV_ENDPOINT} に Argus の URL を指定してください"
        )
    return value


def _store(
    base: str,
    token: dict[str, Any],
    *,
    key_kind: str = "hmac",
    key_ref: str = "",
) -> str:
    """鍵を保存する。非対称の鍵の端末は秘密を受け取らず、鍵の種類と在りかだけを保存する。"""
    device_id = str(token.get("device_id") or "")
    secret = str(token.get("device_secret") or "")
    if not device_id or (key_kind == "hmac" and not secret):
        raise ValueError("応答に device_id か device_secret がありません")
    path = save_credential(
        device_id=device_id,
        secret=secret if key_kind == "hmac" else "",
        endpoint=base,
        tenant_id=str(token.get("tenant_id") or ""),
        expires_at=_epoch(token.get("expires_at")),
        rotate_by=_epoch(token.get("rotate_by")),
        key_kind=key_kind,
        key_ref=key_ref,
    )
    return str(path)


def _provider() -> Optional[KeyProvider]:
    """登録に使う非対称の鍵。使えなければ None を返し、共有の秘密の鍵で登録する。"""
    try:
        return select_provider(default_path().with_name("device-key.pem"))
    except (OSError, ValueError) as exc:
        print(f"端末の鍵を用意できませんでした: {exc.__class__.__name__}", file=sys.stderr)
        return None


def _check_storage_writable() -> None:
    """鍵を書けない場所では、使い切りの登録コードを消費する前に止める。秘密は 1 度しか返らない。"""
    target = default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    probe = target.with_name(target.name + f".{os.getpid()}.probe")
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    finally:
        if probe.exists():
            probe.unlink()


def _approval_url(args: argparse.Namespace, base: str, grant: dict[str, Any]) -> str:
    """承認の画面の URL。画面と API のオリジンが違う配備のため、画面の URL を別に受ける。"""
    dashboard = (
        getattr(args, "dashboard_url", None) or os.environ.get(ENV_DASHBOARD, "")
    ).strip()
    uri = str(grant.get("verification_uri") or "/devices/approve")
    if uri.startswith("/"):
        uri = (dashboard or base).rstrip("/") + uri
    code = str(grant.get("user_code") or "")
    return f"{uri}{'&' if '?' in uri else '?'}user_code={code}" if code else uri


def cmd_register(args: argparse.Namespace) -> int:
    base = _endpoint(args, None)
    _check_storage_writable()
    code = (args.code or os.environ.get(ENV_CODE, "")).strip()
    if not code:
        raise ValueError(f"--code か環境変数 {ENV_CODE} に登録コードを指定してください")
    timeout = float(args.timeout)
    provider = _provider()
    request: dict[str, Any] = {
        "registration_code": code,
        "hostname": hostname(),
        "os_user": os_user(),
        "os": platform.system().lower(),
    }
    if provider is not None:
        try:
            request["public_key"] = provider.public_key_b64()
        except Exception as exc:  # noqa: BLE001 - 鍵を読めないときは共有の秘密の鍵で登録する
            print(f"公開鍵を読めませんでした: {exc.__class__.__name__}", file=sys.stderr)
            provider = None
    status, grant = _post(
        base + "/v1/devices/authorize",
        request,
        timeout=timeout,
    )
    if status != 200 or not grant.get("device_code"):
        print(
            f"登録を始められませんでした: {grant.get('error') or status}",
            file=sys.stderr,
        )
        return 1
    uri = _approval_url(args, base, grant)
    print(
        f"次の URL を開き、SSO でサインインして、コード {grant.get('user_code')} を承認してください。"
    )
    print(uri)
    poll: dict[str, Any] = {"device_code": grant["device_code"]}
    if provider is not None:
        # 証明は登録ごとの challenge を含める。サーバーが検証できたときだけ保護領域の鍵になる。
        try:
            attestation = provider.attest(challenge_for(str(grant["device_code"])))
        except Exception as exc:  # noqa: BLE001 - 証明が無くてもソフトウェアの鍵として登録する
            print(f"端末の証明を作れませんでした: {exc.__class__.__name__}", file=sys.stderr)
            attestation = None
        if attestation is not None:
            poll["attestation"] = attestation
    interval = min(max(float(grant.get("interval") or 5), _MIN_INTERVAL), _MAX_INTERVAL)
    deadline = time.monotonic() + min(float(grant.get("expires_in") or 600), 1800.0)
    network_failed = False
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            status, token = _post(
                base + "/v1/devices/token",
                poll,
                timeout=timeout,
            )
        except _NETWORK_ERRORS:
            # 通信の失敗 1 回で待ちを終えない。登録コードは消費済みで、発行し直しになる。
            network_failed = True
            continue
        if status >= 500 or status == 429:
            # 負荷分散の 502 と 503 と 429 は一時の失敗。登録コードは消費済みで、終えると発行し直しになる。
            continue
        if status == 200:
            try:
                # 秘密が返ったときは、サーバーが公開鍵を受けていない。共有の秘密の鍵として保存する。
                where = (
                    _store(base, token, key_kind=provider.kind, key_ref=provider.ref)
                    if provider is not None and not token.get("device_secret")
                    else _store(base, token)
                )
            except (ValueError, OSError) as exc:
                print(
                    f"承認は完了しましたが、鍵を保存できませんでした: {exc.__class__.__name__}。"
                    "秘密は 1 度しか返らないため、管理者に端末の失効と登録コードの再発行を依頼してください。",
                    file=sys.stderr,
                )
                return 3
            print(f"登録しました。鍵を保存しました: {where}")
            if provider is not None and not token.get("device_secret"):
                print(f"鍵の保護: {token.get('key_protection') or 'unknown'}")
            if os.environ.get("SENDA_ARGUS_DEVICE_ID") and os.environ.get(
                "SENDA_ARGUS_DEVICE_SECRET"
            ):
                print(
                    "環境変数の鍵が残っているため、保存した鍵より環境変数の鍵が使われます。"
                )
            return 0
        error = str(token.get("error") or "")
        if error == "invalid_grant" and network_failed:
            # 応答を受け取り損ねた可能性がある。サーバーは秘密を 1 度しか返さず、端末は有効のまま残る。
            print(
                "承認は完了しましたが、鍵の応答を受け取れませんでした。秘密は 1 度しか返らないため、"
                "管理者に端末の失効と登録コードの再発行を依頼してください。",
                file=sys.stderr,
            )
            return 3
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = min(interval * 2, _MAX_INTERVAL)
            continue
        print(f"登録を完了できませんでした: {error or status}", file=sys.stderr)
        return 1
    print("承認の期限が切れました。登録コードを発行し直してください。", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    credential = load_credential()
    if credential is None and default_path().is_file():
        print(
            "鍵のファイルがありますが使えません。形が壊れているか、他の利用者が読める権限です。"
            "送信は署名なしで、未検証として記録されます。",
        )
        return 1
    if credential is None:
        print(
            "この端末は登録されていません。送信は署名なしで、未検証として記録されます。"
        )
        return 1
    now = time.time()
    print(f"device_id: {credential.device_id}")
    if credential.tenant_id:
        print(f"tenant_id: {credential.tenant_id}")
    if credential.expires_at is not None:
        left = credential.expires_at - now
        print("expires_in_sec: " + ("expired" if left <= 0 else str(int(left))))
    if credential.rotate_by is not None and credential.rotate_by <= now:
        print("更新の期限を過ぎています。senda-hooks device renew を実行してください。")
    return 0 if not credential.expired(now) else 1


def cmd_renew(args: argparse.Namespace) -> int:
    credential = load_credential()
    if credential is None:
        print("この端末は登録されていません。", file=sys.stderr)
        return 1
    if credential.path is None:
        print(
            "環境変数の鍵は更新できません。管理者に新しい鍵を発行してもらってください。",
            file=sys.stderr,
        )
        return 1
    base = _endpoint(args, credential)
    try:
        status, token = _post(
            base + "/v1/devices/renew", {}, timeout=float(args.timeout), signed=True
        )
    except _NETWORK_ERRORS as exc:
        print(
            f"更新の応答を受け取れませんでした: {exc.__class__.__name__}。"
            "サーバーで鍵が入れ替わっていることがあり、前の鍵は 10 分の間だけ更新に使えます。"
            "10 分以内にもう一度 device renew を実行してください。",
            file=sys.stderr,
        )
        return 3
    if status != 200:
        print(f"更新できませんでした: {token.get('error') or status}", file=sys.stderr)
        return 1
    token.setdefault("device_id", credential.device_id)
    token.setdefault("tenant_id", credential.tenant_id)
    try:
        # 非対称の鍵は替えず、期限だけが延びる。鍵の種類と在りかを保つ。
        where = _store(
            base, token, key_kind=credential.key_kind, key_ref=credential.key_ref
        )
    except (ValueError, OSError) as exc:
        print(
            f"鍵は更新されましたが保存できませんでした: {exc.__class__.__name__}。"
            "前の鍵は 10 分の間、更新に使えます。保存先を確認して、もう一度 device renew を実行してください。",
            file=sys.stderr,
        )
        return 3
    print(f"更新しました。鍵を保存しました: {where}")
    return 0


def add_parser(sub: Any) -> None:
    device_p = sub.add_parser(
        "device", help="Register this device with Argus and manage its key"
    )
    device_sub = device_p.add_subparsers(dest="device_command", required=True)

    register_p = device_sub.add_parser(
        "register", help="Register with a one-time code and SSO approval"
    )
    register_p.add_argument("--endpoint", help="Argus base URL")
    register_p.add_argument(
        "--code", help="One-time registration code issued by an admin"
    )
    register_p.add_argument(
        "--dashboard-url",
        help="Dashboard base URL for the approval page when it differs from the API URL",
    )
    register_p.add_argument(
        "--timeout", type=float, default=15.0, help="Seconds per request"
    )
    register_p.set_defaults(func=cmd_register)

    status_p = device_sub.add_parser(
        "status", help="Show the registration of this device"
    )
    status_p.set_defaults(func=cmd_status)

    renew_p = device_sub.add_parser(
        "renew", help="Replace the device key before it expires"
    )
    renew_p.add_argument("--endpoint", help="Argus base URL")
    renew_p.add_argument(
        "--timeout", type=float, default=15.0, help="Seconds per request"
    )
    renew_p.set_defaults(func=cmd_renew)
