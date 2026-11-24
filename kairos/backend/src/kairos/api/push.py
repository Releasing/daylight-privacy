"""Web Push notifications via VAPID (spec p.75, p.116, p.137, p.142) — no paid provider.

Informational only (routine events never wait for user confirmation). Payloads carry no balances, keys or
order details beyond a short human summary. Subscriptions returning 404/410 are removed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger("kairos.push")

PREF_FOR_KIND = {"fill": "fills", "settlement": "settlements", "halt": "halts", "mode": "mode_changes", "notice": "profit_allocations"}


def generate_vapid(secrets_dir: Path) -> str:
    """Create a VAPID key pair; private key 0600 on disk, returns the public key (base64url, uncompressed point)."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    secrets_dir.mkdir(parents=True, exist_ok=True)
    p = secrets_dir / "vapid_private.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    p.chmod(0o600)
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Notifier:
    def __init__(self, store, private_key_path: Path | None, subject: str):
        self.store = store
        self.key = str(private_key_path) if private_key_path and Path(private_key_path).exists() else None
        self.subject = subject

    @property
    def enabled(self) -> bool:
        return self.key is not None

    def summary(self, kind: str, data: dict) -> tuple[str, str] | None:
        if kind == "fill":
            return "Kairos fill", f"{data.get('action', '')} {data.get('qty', '')} {str(data.get('side', '')).upper()} @ {data.get('price', '')}{' (paper)' if data.get('paper') else ''}"
        if kind == "settlement":
            return "Window settled", f"{data.get('ticker', '')}: {str(data.get('result', '')).upper()} · realized {data.get('realized', '')}"
        if kind == "halt":
            return "Kairos halt", f"{data.get('code', '')}: new entries blocked; exits continue"
        if kind == "mode":
            return "Mode change", f"{data.get('from', '')} → {data.get('to', '')}"
        if kind == "notice" and data.get("kind") == "profit_allocation":
            return "Profit protected", f"policy {data.get('policy', '')}"
        return None

    async def notify(self, user_id: int, kind: str, data: dict) -> None:
        if not self.enabled:
            return
        pref = PREF_FOR_KIND.get(kind)
        user = self.store.user(user_id)
        if not pref or not user or not (user.get("notify_prefs") or {}).get(pref, kind in ("halt",)):
            return
        msg = self.summary(kind, data)
        if msg is None:
            return
        payload = json.dumps({"title": msg[0], "body": msg[1][:160], "tag": f"kairos-{kind}"})
        for sub in self.store.pushes(user_id):
            await asyncio.to_thread(self._send, user_id, sub, payload)

    def _send(self, user_id: int, sub: dict, payload: str) -> None:
        from pywebpush import WebPushException, webpush

        try:
            webpush({"endpoint": sub["endpoint"], "keys": sub["keys"]}, payload, vapid_private_key=self.key,
                    vapid_claims={"sub": self.subject}, ttl=300, timeout=5)
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                self.store.remove_push(user_id, sub["endpoint"])
            log.info("push failed: %s", status)
        except Exception as exc:  # never affects trading
            log.info("push error: %s", type(exc).__name__)
