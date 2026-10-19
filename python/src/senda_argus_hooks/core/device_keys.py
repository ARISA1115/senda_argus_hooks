"""端末の非対称の鍵。保護領域の鍵を優先し、無ければソフトウェアの鍵を使う。

サーバーは ECDSA の公開鍵だけを持ち、秘密鍵を受け渡さない。鍵が保護領域にあるかは、登録のときの証明を
サーバーが検証した結果だけで決まる。この部品が保護領域と申告しても、サーバーは採らない。

鍵の置き場は 2 つ。

- 鍵の補助のコマンド: ``SENDA_ARGUS_DEVICE_KEY_HELPER`` に置いたコマンド。TPM、Secure Enclave、Platform
  Crypto Provider の鍵を扱う部品を、配備がこの形で置く。秘密鍵は保護領域の外へ出ない。次の 3 つの
  呼び出しに答える

      <helper> public-key          標準出力に SubjectPublicKeyInfo の DER の base64
      <helper> sign                標準入力の本文に対する DER の ECDSA 署名の base64
      <helper> attest <challenge>  challenge (16 進) を含めた証明の JSON。証明の無い鍵は空

- ソフトウェアの鍵: ``cryptography`` が使えるときに、利用者だけが読めるファイルに P-256 の鍵を作る。
  サーバーはソフトウェアの鍵として区別する

どちらも使えなければ None を返し、呼び出し側は共有の秘密の鍵の手順に戻る。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final, Optional, Protocol

ENV_HELPER: Final[str] = "SENDA_ARGUS_DEVICE_KEY_HELPER"
KIND_HELPER: Final[str] = "helper"
KIND_SOFTWARE: Final[str] = "software"
CHALLENGE_CONTEXT: Final[bytes] = b"argus-device-attest-v1\n"
_HELPER_TIMEOUT_SEC: Final[float] = 10.0
_MAX_OUTPUT: Final[int] = 65_536


class KeyProvider(Protocol):
    kind: str
    ref: str

    def public_key_b64(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...

    def attest(self, challenge: bytes) -> Optional[dict[str, Any]]: ...


def challenge_for(device_code: str) -> bytes:
    """サーバーと同じ challenge。device_code はサーバーが作る 1 度限りの値。"""
    return hashlib.sha256(CHALLENGE_CONTEXT + device_code.encode("utf-8")).digest()


class HelperKey:
    kind = KIND_HELPER

    def __init__(self, command: str) -> None:
        self.ref = command

    def _run(self, *args: str, stdin: bytes = b"") -> bytes:
        done = subprocess.run(  # noqa: S603 - 配備が置いたコマンドを引数の並びで呼ぶ
            [self.ref, *args],
            input=stdin,
            capture_output=True,
            timeout=_HELPER_TIMEOUT_SEC,
            check=True,
        )
        return done.stdout[:_MAX_OUTPUT].strip()

    def public_key_b64(self) -> str:
        return self._run("public-key").decode("ascii")

    def sign(self, message: bytes) -> bytes:
        return base64.b64decode(self._run("sign", stdin=message), validate=True)

    def attest(self, challenge: bytes) -> Optional[dict[str, Any]]:
        raw = self._run("attest", challenge.hex())
        if not raw:
            return None
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None


class SoftwareKey:
    kind = KIND_SOFTWARE

    def __init__(self, path: Path) -> None:
        self.ref = str(path)
        self._key = _load_or_create(path)

    def public_key_b64(self) -> str:
        from cryptography.hazmat.primitives import serialization

        der = self._key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(der).decode("ascii")

    def sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        return self._key.sign(message, ec.ECDSA(hashes.SHA256()))

    def attest(self, challenge: bytes) -> Optional[dict[str, Any]]:
        # ソフトウェアの鍵に証明は無い。サーバーはソフトウェアの鍵として登録する。
        return None


def _load_or_create(path: Path) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if path.is_file():
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise PermissionError("device key file is readable by other users")
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
        handle.flush()
        os.fsync(handle.fileno())
    return key


def software_available() -> bool:
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
    except ImportError:
        return False
    return True


def select_provider(key_path: Path) -> Optional[KeyProvider]:
    """登録に使う鍵を選ぶ。補助のコマンドを優先し、無ければソフトウェアの鍵、どちらも無ければ None。"""
    helper = os.environ.get(ENV_HELPER, "").strip()
    if helper:
        return HelperKey(helper)
    if software_available():
        return SoftwareKey(key_path)
    return None


def provider_for(kind: str, ref: str) -> Optional[KeyProvider]:
    """保存した鍵の種類と参照から、署名に使う鍵を開く。開けなければ None。"""
    try:
        if kind == KIND_HELPER and ref:
            return HelperKey(ref)
        if kind == KIND_SOFTWARE and ref and software_available():
            return SoftwareKey(Path(ref))
    except (OSError, ValueError):
        return None
    return None


def signature_b64url(provider: KeyProvider, message: bytes) -> str:
    return base64.urlsafe_b64encode(provider.sign(message)).decode("ascii").rstrip("=")
