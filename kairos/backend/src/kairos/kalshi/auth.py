"""Kalshi request signing (spec p.4).

RSA-PSS / SHA-256 / MGF1(SHA-256) / salt = digest length over
``timestamp_ms + METHOD + path`` where ``path`` includes the ``/trade-api/v2`` prefix and excludes the
query string. The private key is loaded from a file path (0600) and never logged or returned.
"""
from __future__ import annotations

import base64
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class AuthError(RuntimeError):
    pass


def signing_path(url_or_path: str) -> str:
    """Return the path component to sign: no scheme/host, no query string, no fragment."""
    parts = urlsplit(url_or_path)
    path = parts.path if (parts.scheme or parts.netloc) else url_or_path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        raise AuthError("signing path must be absolute")
    return path


def message(timestamp_ms: int, method: str, path: str) -> bytes:
    return f"{timestamp_ms}{method.upper()}{signing_path(path)}".encode()


@dataclass(frozen=True)
class KalshiSigner:
    key_id: str
    _key: rsa.RSAPrivateKey

    def __repr__(self) -> str:  # never leak key material
        return f"KalshiSigner(key_id={self.key_id[:4]}…)"

    @classmethod
    def from_pem(cls, key_id: str, pem: bytes, password: bytes | None = None) -> "KalshiSigner":
        key = serialization.load_pem_private_key(pem, password=password)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise AuthError("Kalshi key must be RSA for RSA-PSS signing")
        if not key_id:
            raise AuthError("missing key id")
        return cls(key_id=key_id, _key=key)

    @classmethod
    def from_file(cls, key_id: str, path: str | os.PathLike[str], enforce_mode: bool = True) -> "KalshiSigner":
        p = Path(path)
        st = p.stat()
        if enforce_mode and (st.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise AuthError(f"private key {p.name} must have permissions 0600")
        return cls.from_pem(key_id, p.read_bytes())

    def sign(self, timestamp_ms: int, method: str, path: str) -> str:
        sig = self._key.sign(
            message(timestamp_ms, method, path),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def headers(self, method: str, path: str, now_ms: int | None = None) -> dict[str, str]:
        ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }

    def public_key(self) -> rsa.RSAPublicKey:
        return self._key.public_key()


def verify(public_key: rsa.RSAPublicKey, signature_b64: str, timestamp_ms: int, method: str, path: str) -> bool:
    try:
        public_key.verify(
            base64.b64decode(signature_b64),
            message(timestamp_ms, method, path),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False
