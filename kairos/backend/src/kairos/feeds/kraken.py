"""Kraken WebSocket v2 public adapter (spec p.3). Channels ``ticker`` and ``trade``.
Kraken ``side`` on trades is the taker (aggressor) side per v2 docs (unverified in sandbox)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..kalshi.parse import parse_ts
from .base import Quote, Trade

log = logging.getLogger("kairos.feeds.kraken")

URL = "wss://ws.kraken.com/v2"
SYMBOLS = {"BTC/USD": "BTC", "ETH/USD": "ETH", "SOL/USD": "SOL"}


def normalize(msg: dict[str, Any], receive_ts: float) -> tuple[list[Quote], list[Trade]]:
    quotes: list[Quote] = []
    trades: list[Trade] = []
    ch = msg.get("channel")
    if msg.get("type") not in ("snapshot", "update"):
        return quotes, trades
    for d in msg.get("data", []) or []:
        asset = SYMBOLS.get(d.get("symbol", ""))
        if not asset:
            continue
        if ch == "ticker":
            try:
                bid, ask = float(d["bid"]), float(d["ask"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 < bid <= ask:
                quotes.append(Quote("kraken", asset, bid, ask, None, receive_ts))
        elif ch == "trade":
            try:
                px, sz = float(d["price"]), float(d["qty"])
            except (KeyError, TypeError, ValueError):
                continue
            ts = parse_ts(d.get("timestamp"))
            side = d.get("side") if d.get("side") in ("buy", "sell") else None
            if px > 0 and sz > 0:
                trades.append(Trade("kraken", asset, px, sz, side, ts.timestamp() if ts else None, receive_ts, str(d.get("trade_id", ""))))
    return quotes, trades


class KrakenFeed:
    venue = "kraken"

    def __init__(self, on_quote: Callable[[Quote], Awaitable[None]], on_trade: Callable[[Trade], Awaitable[None]],
                 on_raw: Callable[[str, dict, float], Awaitable[None]] | None = None, symbols: tuple[str, ...] = tuple(SYMBOLS)):
        self.on_quote, self.on_trade, self.on_raw = on_quote, on_trade, on_raw
        self.symbols = symbols
        self.connected = False
        self._stop = asyncio.Event()

    async def run(self) -> None:
        import websockets

        from ..kalshi.rest import backoff_delay

        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(URL, ping_interval=15, max_size=2**22) as ws:
                    self.connected, attempt = True, 0
                    for ch in ("ticker", "trade"):
                        await ws.send(json.dumps({"method": "subscribe", "params": {"channel": ch, "symbol": list(self.symbols)}}))
                    async for raw in ws:
                        rts = time.time()
                        msg = json.loads(raw)
                        if self.on_raw:
                            await self.on_raw(self.venue, msg, rts)
                        qs, ts = normalize(msg, rts)
                        for q in qs:
                            await self.on_quote(q)
                        for t in ts:
                            await self.on_trade(t)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("kraken ws error: %s", type(exc).__name__)
            finally:
                self.connected = False
            await asyncio.sleep(backoff_delay(attempt, base=0.5, cap=30))
            attempt += 1

    def stop(self) -> None:
        self._stop.set()
