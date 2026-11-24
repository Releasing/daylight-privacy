"""Order-flow and liquidity features (spec p.7, p.47, p.104)."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from ..market.book import OrderBook


@dataclass
class TradeFlow:
    """Signed trade imbalance over rolling windows. Zero denominator → None (missing, not bullish)."""

    windows: tuple[int, ...] = (5, 15, 60)
    trades: deque = field(default_factory=lambda: deque(maxlen=20000))

    def add(self, ts: float, size: float, side: str | None) -> None:
        if side in ("buy", "sell") and size > 0:
            self.trades.append((ts, size if side == "buy" else -size))

    def imbalance(self, now: float, window: int) -> float | None:
        buy = sell = 0.0
        for ts, s in reversed(self.trades):
            if now - ts > window:
                break
            if s > 0:
                buy += s
            else:
                sell -= s
        tot = buy + sell
        return (buy - sell) / tot if tot > 0 else None

    def volume_delta(self, now: float, window: int) -> float | None:
        vals = [s for ts, s in self.trades if now - ts <= window]
        return sum(vals) if vals else None

    def features(self, now: float) -> dict[str, float | None]:
        out = {f"flow_imb_{w}s": self.imbalance(now, w) for w in self.windows}
        out["vol_delta_60s"] = self.volume_delta(now, 60)
        return out


def book_features(book: OrderBook | None, band: Decimal = Decimal("0.03")) -> dict[str, float | None]:
    if book is None or not book.valid:
        return {"k_spread": None, "k_depth_imb": None, "k_microprice": None, "k_mid": None}
    bd, ad = book.depth_within("yes", band)
    imb = float((bd - ad) / (bd + ad)) if (bd + ad) > 0 else None
    sp = book.spread_yes()
    mp = book.microprice_yes()
    mid = book.mid_yes()
    return {
        "k_spread": float(sp) if sp is not None else None,
        "k_depth_imb": imb,
        "k_microprice": float(mp) if mp is not None else None,
        "k_mid": float(mid) if mid is not None else None,
    }
