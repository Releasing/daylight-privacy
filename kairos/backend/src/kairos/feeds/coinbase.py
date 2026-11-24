"""Coinbase Advanced Trade public WebSocket adapter (spec p.3).

Channels: ``ticker``, ``market_trades``, ``heartbeats``. Aggressor semantics: Coinbase ``side`` on
market_trades is the maker side in some feeds and the taker side in others; the adapter keeps a
configurable mapping (``side_is_taker``) and defaults to *unknown* aggressor until verified on the VPS
(p.7 "Confirm venue aggressor semantics").
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..kalshi.parse import parse_ts
from .base import Quote, Trade

log = logging.getLogger("kairos.feeds.coinbase")

URL = "wss://advanced-trade-ws.coinbase.com"
PRODUCTS = {"BTC-USD": "BTC", "ETH-USD": "ETH", "SOL-USD": "SOL"}


def normalize(msg: dict[str, Any], receive_ts: float, side_is_taker: bool | None = None) -> tuple[list[Quote], list[Trade], int | None]:
    quotes: list[Quote] = []
    trades: list[Trade] = []
    seq = msg.get("sequence_num")
    ch = msg.get("channel")
    ts = parse_ts(msg.get("timestamp"))
    ets = ts.timestamp() if ts else None
    for ev in msg.get("events", []) or []:
        if ch == "ticker":
            for t in ev.get("tickers", []) or []:
                asset = PRODUCTS.get(t.get("product_id", ""))
                try:
                    bid, ask = float(t["best_bid"]), float(t["best_ask"])
                except (KeyError, TypeError, ValueError):
                    continue
                if asset and 0 < bid <= ask:
                    quotes.append(Quote("coinbase", asset, bid, ask, ets, receive_ts, seq))
        elif ch == "market_trades":
            for t in ev.get("trades", []) or []:
                asset = PRODUCTS.get(t.get("product_id", ""))
                try:
                    px, sz = float(t["price"]), float(t["size"])
                except (KeyError, TypeError, ValueError):
                    continue
                side = None
                if side_is_taker is not None and t.get("side") in ("BUY", "SELL"):
                    s = t["side"].lower()
                    side = s if side_is_taker else ("sell" if s == "buy" else "buy")
                tt = parse_ts(t.get("time"))
                if asset and px > 0 and sz > 0:
                    trades.append(Trade("coinbase", asset, px, sz, side, tt.timestamp() if tt else ets, receive_ts, str(t.get("trade_id", ""))))
    return quotes, trades, seq


class CoinbaseFeed:
    venue = "coinbase"

    def __init__(self, on_quote: Callable[[Quote], Awaitable[None]], on_trade: Callable[[Trade], Awaitable[None]],
                 on_raw: Callable[[str, dict, float], Awaitable[None]] | None = None, products: tuple[str, ...] = tuple(PRODUCTS)):
        self.on_quote, self.on_trade, self.on_raw = on_quote, on_trade, on_raw
        self.products = products
        self.last_seq: int | None = None
        self.gaps = 0
        self.connected = False
        self._stop = asyncio.Event()

    async def run(self) -> None:
        import websockets

        from ..kalshi.rest import backoff_delay

        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(URL, ping_interval=15, max_size=2**22) as ws:
                    self.connected, attempt, self.last_seq = True, 0, None
                    for ch in ("heartbeats", "ticker", "market_trades"):
                        await ws.send(json.dumps({"type": "subscribe", "product_ids": list(self.products), "channel": ch}))
                    async for raw in ws:
                        rts = time.time()
                        msg = json.loads(raw)
                        if self.on_raw:
                            await self.on_raw(self.venue, msg, rts)
                        qs, ts, seq = normalize(msg, rts)
                        if seq is not None:
                            if self.last_seq is not None and seq != self.last_seq + 1:
                                self.gaps += 1
                            self.last_seq = seq
                        for q in qs:
                            await self.on_quote(q)
                        for t in ts:
                            await self.on_trade(t)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("coinbase ws error: %s", type(exc).__name__)
            finally:
                self.connected = False
            await asyncio.sleep(backoff_delay(attempt, base=0.5, cap=30))
            attempt += 1

    def stop(self) -> None:
        self._stop.set()
