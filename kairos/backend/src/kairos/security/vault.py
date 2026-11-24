"""Secrets handling (spec p.4, p.37, p.49, p.115, p.137).

* Per-tenant credentials are encrypted at rest with AES-256-GCM under a server master key read from a
  0600 file (``KAIROS_MASTER_KEY_FILE``); the key never enters the database, logs or the browser.
* Passwords: Argon2id. MFA: RFC 6238 TOTP (secret encrypted at rest).
* A logging filter redacts anything that looks like key material.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import stat
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise VaultError("master key must be 32 bytes")
        self._aes = AESGCM(key)

    def __repr__(self) -> str:
        return "Vault(<redacted>)"

    @classmethod
    def from_file(cls, path: str | os.PathLike[str], create: bool = False) -> "Vault":
        p = Path(path)
        if not p.exists():
            if not create:
                raise VaultError("master key file missing")
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64encode(secrets.token_bytes(32)))
        if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise VaultError("master key file must be 0600")
        return cls(base64.b64decode(p.read_bytes().strip()))

    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> str:
        nonce = secrets.token_bytes(12)
        return base64.b64encode(nonce + self._aes.encrypt(nonce, plaintext, aad)).decode()

    def decrypt(self, token: str, aad: bytes = b"") -> bytes:
        raw = base64.b64decode(token)
        try:
            return self._aes.decrypt(raw[:12], raw[12:], aad)
        except Exception as exc:
            raise VaultError("decryption failed") from exc


_PH = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1)


def hash_password(pw: str) -> str:
    if len(pw) < 12:
        raise VaultError("password must be at least 12 characters")
    return _PH.hash(pw)


def verify_password(h: str, pw: str) -> bool:
    try:
        return _PH.verify(h, pw)
    except (VerifyMismatchError, VerificationError, Exception):
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str, now: float | None = None) -> bool:
    if not code or not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, for_time=now, valid_window=1)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_REDACT = re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|"
                     r"(KALSHI-ACCESS-SIGNATURE|password|secret|api[_-]?key|authorization)[\"'=:\s]+[^\s,\"'}]+)", re.I)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = _REDACT.sub("[REDACTED]", msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def install_log_redaction() -> None:
    root = logging.getLogger()
    for h in root.handlers or [logging.StreamHandler()]:
        h.addFilter(RedactingFilter())
    if not root.handlers:
        h = logging.StreamHandler()
        h.addFilter(RedactingFilter())
        root.addHandler(h)
