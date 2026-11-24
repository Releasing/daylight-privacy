"""Normalized external market events (spec p.3, p.6)."""
from __future__ import annotations

from dataclasses import dataclass

ASSETS = ("BTC", "ETH", "SOL")


@dataclass(frozen=True)
class Quote:
    venue: str
    asset: str
    bid: float
    ask: float
    exchange_ts: float | None
    receive_ts: float
    seq: int | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class Trade:
    venue: str
    asset: str
    price: float
    size: float
    side: str | None  # aggressor side: "buy" | "sell" | None (unknown)
    exchange_ts: float | None
    receive_ts: float
    trade_id: str = ""
