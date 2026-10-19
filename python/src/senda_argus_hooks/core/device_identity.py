"""端末の登録鍵による送信の署名。

組織の API キーは、持つ者が誰でも名乗れる資格情報で、端末を特定しない。端末ごとの登録鍵で本文を署名すると、
受け取り側は、どの端末が送ったかを署名の検証だけで決められる。

- 鍵は端末の登録の手順 (cli/device.py) で受け取り、利用者だけが読めるファイルへ保存する。CI やコンテナの
  ようにファイルを置けない環境は、環境変数 SENDA_ARGUS_DEVICE_ID と SENDA_ARGUS_DEVICE_SECRET に、期限つきの
  ワークロードの鍵を置く
- 署名は本文のバイト列に対して付ける。ヘッダーの値は、受け取り側が検証済みかどうかを決める材料ではなく、
  署名の検証の結果だけが決める。OS の利用者名と hostname は自己申告として送る
- 署名が無い送信は、受け取り側が移行中の組織では受理され、未検証として記録される。鍵が無い、読めない、
  壊れている場合も、送信は止めず、署名を付けない
- 共有の秘密の鍵は標準ライブラリだけで動き、HMAC-SHA256 を付ける
- 非対称の鍵の端末は、core/device_keys.py の鍵で同じ文字列に ECDSA の署名を付ける。秘密をサーバーと
  共有しないため、鍵を取り出せない保護領域に置ける。鍵の種類は登録の時に決まり、鍵のファイルに残す
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Optional
from urllib.parse import quote

try:  # POSIX だけ。無い環境では、プロセス内の通し番号に倒す。
    import fcntl
    import pwd
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]

SIGNATURE_VERSION: Final[str] = "argus-device-v1"
HEADER_ID: Final[str] = "X-Argus-Device-Id"
HEADER_TS: Final[str] = "X-Argus-Device-Ts"
HEADER_SEQ: Final[str] = "X-Argus-Device-Seq"
HEADER_SIG: Final[str] = "X-Argus-Device-Sig"
HEADER_USER: Final[str] = "X-Argus-Device-User"
HEADER_HOST: Final[str] = "X-Argus-Device-Host"

ENV_FILE: Final[str] = "SENDA_ARGUS_DEVICE_FILE"
ENV_DEVICE_ID: Final[str] = "SENDA_ARGUS_DEVICE_ID"
ENV_DEVICE_SECRET: Final[str] = "SENDA_ARGUS_DEVICE_SECRET"
ENV_DEVICE_EXPIRES_AT: Final[str] = "SENDA_ARGUS_DEVICE_EXPIRES_AT"

_MAX_FILE_BYTES: Final[int] = 16_384
_MAX_FIELD_LEN: Final[int] = 512
_HEADER_LIMIT = 256
_LOCK_TRIES = 20
_LOCK_WAIT_SEC = 0.01
_warned_expired = False
_warn_lock = threading.Lock()


@dataclass(frozen=True)
class DeviceCredential:
    device_id: str
    secret: str = field(repr=False)
    expires_at: Optional[float] = None
    rotate_by: Optional[float] = None
    endpoint: str = ""
    tenant_id: str = ""
    path: Optional[Path] = None
    # hmac は共有の秘密。helper と software は core/device_keys.py の非対称の鍵で、key_ref が鍵の在りか。
    key_kind: str = "hmac"
    key_ref: str = ""

    def expired(self, now: Optional[float] = None) -> bool:
        return self.expires_at is not None and (now or time.time()) >= self.expires_at


def default_path() -> Path:
    override = os.environ.get(ENV_FILE, "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "senda-argus" / "device.json"


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _text(value: Any) -> str:
    return value.strip()[:_MAX_FIELD_LEN] if isinstance(value, str) else ""


# 端末の識別子は、ヘッダーへそのまま入れる。改行や制御文字を含むと、送信の部品が例外を上げる。
_DEVICE_ID_PATTERN = re.compile(r"^[\x21-\x7e]{1,256}$")
# 秘密は 32 バイトを hex にした 64 文字。短い hex や hex でない値は、受け取り側が検証できない署名になる。
_SECRET_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


KEY_KIND_HMAC: Final[str] = "hmac"
_ASYMMETRIC_KINDS: Final[frozenset[str]] = frozenset({"helper", "software"})


def _usable(device_id: str, secret: str, key_kind: str = KEY_KIND_HMAC, key_ref: str = "") -> bool:
    if not _DEVICE_ID_PATTERN.match(device_id):
        return False
    if key_kind in _ASYMMETRIC_KINDS:
        return bool(key_ref)
    return key_kind == KEY_KIND_HMAC and bool(_SECRET_PATTERN.match(secret))


def _from_env() -> Optional[DeviceCredential]:
    device_id = _text(os.environ.get(ENV_DEVICE_ID))
    secret = _text(os.environ.get(ENV_DEVICE_SECRET))
    if not _usable(device_id, secret):
        return None
    raw = os.environ.get(ENV_DEVICE_EXPIRES_AT, "").strip()
    try:
        expires = float(raw) if raw else None
    except ValueError:
        expires = None
    return DeviceCredential(device_id=device_id, secret=secret, expires_at=expires)


def _readable_only_by_owner(path: Path) -> bool:
    """他の利用者が読める鍵のファイルは使わない。読めた鍵は既に漏れている前提で、署名を付けない。"""
    if os.name != "posix":
        return True
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))


def load_credential() -> Optional[DeviceCredential]:
    """保存した鍵を読む。環境変数の鍵を先に読む。読めないときは None を返し、例外を上げない。"""
    from_env = _from_env()
    if from_env is not None:
        return from_env
    path = default_path()
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return None
        if not _readable_only_by_owner(path):
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    device_id = _text(data.get("device_id"))
    secret = _text(data.get("device_secret"))
    key_kind = _text(data.get("key_kind")) or KEY_KIND_HMAC
    key_ref = _text(data.get("key_ref"))
    if not _usable(device_id, secret, key_kind, key_ref):
        return None
    return DeviceCredential(
        device_id=device_id,
        secret=secret,
        expires_at=_number(data.get("expires_at")),
        rotate_by=_number(data.get("rotate_by")),
        endpoint=_text(data.get("endpoint")),
        tenant_id=_text(data.get("tenant_id")),
        path=path,
        key_kind=key_kind,
        key_ref=key_ref,
    )


def save_credential(
    *,
    device_id: str,
    secret: str,
    endpoint: str,
    tenant_id: str,
    expires_at: Optional[float],
    rotate_by: Optional[float],
    path: Optional[Path] = None,
    key_kind: str = KEY_KIND_HMAC,
    key_ref: str = "",
) -> Path:
    """鍵を保存する。作成の時点で利用者だけが読める権限にし、作った後に権限を絞る隙間を作らない。"""
    target = path or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "device_id": device_id,
            "device_secret": secret,
            "endpoint": endpoint,
            "tenant_id": tenant_id,
            "expires_at": expires_at,
            "rotate_by": rotate_by,
            "key_kind": key_kind,
            "key_ref": key_ref,
        }
    ).encode("utf-8")
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            # 秘密は 1 度しか返らない。電源が落ちて空のファイルが残らないよう、置き換えの前に書き出す。
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return target


def encode_header_value(value: str, limit: int = _HEADER_LIMIT) -> str:
    """自己申告のヘッダーの値を UTF-8 の percent エンコードにする。

    非 ASCII の利用者名や hostname をそのまま入れると、送信が例外で失敗する。受け取り側は同じ規則で戻す。
    符号化の後の長さが上限を超える前で切る。上限を超えると、受け取り側は署名ごと拒否する。
    """
    out = ""
    for ch in value:
        piece = quote(ch, safe="")
        if len(out) + len(piece) > limit:
            break
        out += piece
    return out


def os_user() -> str:
    try:
        if pwd is not None:
            # 環境変数 USER と LOGNAME は書き換えられ、JavaScript の SDK が読む passwd の値と食い違う。
            return pwd.getpwuid(os.getuid()).pw_name[:_MAX_FIELD_LEN]
        return getpass.getuser()[:_MAX_FIELD_LEN]
    except Exception:  # noqa: BLE001 - 利用者名を引けない環境でも送信は止めない
        return ""


def hostname() -> str:
    try:
        return socket.gethostname()[:_MAX_FIELD_LEN]
    except OSError:
        return ""


def _lock_with_retry(fd: int) -> bool:
    """排他の取得を短く待つ。取れなければ諦め、判定の時間の予算の外で止まらないようにする。"""
    for _ in range(_LOCK_TRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            time.sleep(_LOCK_WAIT_SEC)
    return False


def _warn_expired_once(credential: DeviceCredential) -> None:
    global _warned_expired
    with _warn_lock:
        if _warned_expired:
            return
        _warned_expired = True
    print(
        f"senda-argus: device key {credential.device_id} has expired; sends are rejected until it is replaced",
        file=sys.stderr,
    )


def _next_seq(credential: DeviceCredential) -> int:
    """端末ごとの通し番号。ファイルの鍵は、複数のプロセスが同じ番号を使わないよう、排他で増やす。"""
    if credential.path is not None and fcntl is not None:
        counter = credential.path.with_name(credential.path.name + ".seq")
        try:
            fd = os.open(counter, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if not _lock_with_retry(fd):
                    return secrets.randbits(63)
                raw = os.read(fd, 32)
                last = int(raw.decode("ascii") or "0") if raw else 0
                value = last + 1
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, str(value).encode("ascii"))
                return value
            finally:
                os.close(fd)
        except (OSError, ValueError):
            pass
    # 排他できない経路は、別のプロセスと同じ番号にならないよう乱数にする。受け取り側は、見た番号の集合で判定する。
    return secrets.randbits(63)


def signing_material(device_id: str, ts: int, seq: int, body: bytes) -> str:
    """署名の対象。共有の秘密の鍵と非対称の鍵で同じ文字列を使い、受け取り側の式と一致させる。"""
    return "\n".join(
        (
            SIGNATURE_VERSION,
            device_id,
            str(ts),
            str(seq),
            hashlib.sha256(body).hexdigest(),
        )
    )


def signature_of(secret: str, device_id: str, ts: int, seq: int, body: bytes) -> str:
    material = signing_material(device_id, ts, seq, body)
    # 秘密は 32 バイトを hex にした文字列で渡される。鍵は hex を戻したバイト列で、受け取り側と同じにする。
    return hmac.new(
        bytes.fromhex(secret), material.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def reserve_seq() -> Optional[int]:
    """再送のあいだ同じ通し番号を使うため、送信の前に 1 回だけ採る。鍵が無いときは None を返す。"""
    cred = load_credential()
    return None if cred is None else _next_seq(cred)


def sign_headers(
    body: bytes,
    *,
    credential: Optional[DeviceCredential] = None,
    seq: Optional[int] = None,
) -> dict[str, str]:
    """本文に付ける署名のヘッダー。鍵が無いときは空を返す。

    送信のたびに呼ぶ。再送は新しい時刻と通し番号で署名し直す。時刻の鮮度は受け取り側のサーバーの時計で見る。
    """
    cred = credential if credential is not None else load_credential()
    if cred is None:
        return {}
    ts = int(time.time())
    seq = seq if seq is not None else _next_seq(cred)
    if cred.key_kind in _ASYMMETRIC_KINDS:
        from senda_argus_hooks.core.device_keys import provider_for, signature_b64url

        provider = provider_for(cred.key_kind, cred.key_ref)
        if provider is None:
            return {}
        try:
            signature = signature_b64url(
                provider, signing_material(cred.device_id, ts, seq, body).encode("utf-8")
            )
        except Exception:  # noqa: BLE001 - 鍵を使えない間も送信は止めず、署名なしで送る
            return {}
    else:
        try:
            signature = signature_of(cred.secret, cred.device_id, ts, seq, body)
        except ValueError:
            # hex として読めない秘密では署名できない。送信は止めず、署名なしで送る。
            return {}
    if cred.expired():
        _warn_expired_once(cred)
    headers = {
        HEADER_ID: cred.device_id,
        HEADER_TS: str(ts),
        HEADER_SEQ: str(seq),
        HEADER_SIG: signature,
    }
    user = os_user()
    host = hostname()
    if user:
        headers[HEADER_USER] = encode_header_value(user)
    if host:
        headers[HEADER_HOST] = encode_header_value(host)
    return headers
