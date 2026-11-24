"""Authenticated Kalshi WebSocket client (spec p.3, p.6, p.88, p.133).

Subscribes to ``orderbook_delta`` (snapshot then sequenced deltas), ``ticker`` and ``trade`` for the
active tickers. On sequence gaps the affected book is marked stale and the subscription is re-created to
obtain a fresh snapshot. Reconnects use exponential backoff with jitter. Messages are timestamped on
receipt with wall-clock and monotonic clocks before any processing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .auth import KalshiSigner
from .rest import backoff_delay

log = logging.getLogger("kairos.kalshi.ws")

Handler = Callable[[dict[str, Any], float, int], Awaitable[None]]


class KalshiWS:
    def __init__(self, url: str, signer: KalshiSigner, on_message: Handler, channels: tuple[str, ...] = ("orderbook_delta", "ticker", "trade")):
        self.url = url
        self.signer = signer
        self.on_message = on_message
        self.channels = channels
        self.tickers: set[str] = set()
        self._ws: Any = None
        self._next_id = 1
        self._stop = asyncio.Event()
        self.connected = False
        self.connection_id = 0
        self.last_recv_mono: float | None = None
        self._sids: dict[str, int] = {}

    @property
    def path(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.url).path

    async def set_tickers(self, tickers: set[str]) -> None:
        added = tickers - self.tickers
        removed = self.tickers - tickers
        self.tickers = set(tickers)
        if self._ws is not None and self.connected:
            if removed:
                await self._unsubscribe_all()
                await self._subscribe(self.tickers)
            elif added:
                await self._subscribe(added)

    async def resync(self, ticker: str) -> None:
        """Force a fresh snapshot after a sequence gap: unsubscribe + resubscribe."""
        if self._ws is None:
            return
        await self._unsubscribe_all()
        await self._subscribe(self.tickers)

    async def _send(self, payload: dict[str, Any]) -> None:
        payload["id"] = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps(payload))

    async def _subscribe(self, tickers: set[str]) -> None:
        if not tickers:
            return
        await self._send({"cmd": "subscribe", "params": {"channels": list(self.channels), "market_tickers": sorted(tickers)}})

    async def _unsubscribe_all(self) -> None:
        if self._sids:
            await self._send({"cmd": "unsubscribe", "params": {"sids": sorted(set(self._sids.values()))}})
            self._sids.clear()

    async def run(self) -> None:
        import websockets

        attempt = 0
        while not self._stop.is_set():
            headers = self.signer.headers("GET", self.path)
            try:
                async with websockets.connect(self.url, additional_headers=headers, ping_interval=10, ping_timeout=10, max_size=2**22) as ws:
                    self._ws = ws
                    self.connected = True
                    self.connection_id += 1
                    attempt = 0
                    await self._subscribe(self.tickers)
                    async for raw in ws:
                        recv_wall = time.time()
                        recv_mono = time.monotonic_ns()
                        self.last_recv_mono = time.monotonic()
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("non-json ws message dropped (quarantined)")
                            continue
                        if msg.get("type") == "subscribed":
                            m = msg.get("msg", {})
                            self._sids[str(m.get("channel"))] = int(m.get("sid", 0))
                        await self.on_message(msg, recv_wall, recv_mono)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors → reconnect with backoff
                log.warning("kalshi ws disconnected: %s", type(exc).__name__)
            finally:
                self.connected = False
                self._ws = None
                self._sids.clear()
            await asyncio.sleep(backoff_delay(attempt, base=0.5, cap=30))
            attempt += 1

    def stop(self) -> None:
        self._stop.set()
