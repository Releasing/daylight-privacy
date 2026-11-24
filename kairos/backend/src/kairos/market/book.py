"""Kalshi binary order book (spec p.6, p.13, p.29, p.133).

Kalshi publishes **bids only** for YES and NO. A NO bid at x is a YES ask at 1 − x.
Executable prices are computed by walking visible depth for the requested quantity; missing liquidity is
never inferred and stale/invalid books return ``None`` (never zero or synthetic).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from ..kalshi.parse import BookDelta, BookSnapshot
from ..money import ONE, ZERO


class BookError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fillable:
    quantity: Decimal  # fillable quantity (≤ requested)
    cost: Decimal  # Σ q_i · p_i  (dollars, before fees)
    vwap: Decimal | None
    worst: Decimal | None
    levels: tuple[tuple[Decimal, Decimal], ...]

    @property
    def complete(self) -> bool:
        return self.quantity > 0


@dataclass
class OrderBook:
    ticker: str
    yes: dict[Decimal, Decimal] = field(default_factory=dict)  # price → qty (bids)
    no: dict[Decimal, Decimal] = field(default_factory=dict)
    seq: int | None = None
    valid: bool = False
    stale_reason: str | None = "no_snapshot"
    exchange_ts: float | None = None
    receive_ts: float | None = None
    receive_mono: float | None = None
    updates: int = 0
    # liquidity integrity (p.46–47): adds/cancels per side
    added_qty: Decimal = ZERO
    removed_qty: Decimal = ZERO

    # ---- maintenance -----------------------------------------------------------------------
    def apply_snapshot(self, snap: BookSnapshot, seq: int | None, receive_ts: float, exchange_ts: float | None = None) -> None:
        self.yes = {p: q for p, q in snap.yes_bids if q > 0}
        self.no = {p: q for p, q in snap.no_bids if q > 0}
        self.seq = seq
        self.receive_ts = receive_ts
        self.receive_mono = time.monotonic()
        self.exchange_ts = exchange_ts
        self.updates += 1
        self.valid = True
        self.stale_reason = None
        if self.crossed():
            self.invalidate("crossed_book")

    def apply_delta(self, d: BookDelta, seq: int | None, receive_ts: float, exchange_ts: float | None = None) -> bool:
        """Apply a sequenced delta. Returns False (and invalidates) on a sequence gap."""
        if not self.valid:
            return False
        if seq is not None and self.seq is not None and seq != self.seq + 1:
            self.invalidate(f"seq_gap:{self.seq}->{seq}")
            return False
        side = self.yes if d.side == "yes" else self.no
        cur = side.get(d.price, ZERO)
        new = cur + d.delta
        if new < 0:
            self.invalidate("negative_depth")
            return False
        if d.delta > 0:
            self.added_qty += d.delta
        else:
            self.removed_qty += -d.delta
        if new == 0:
            side.pop(d.price, None)
        else:
            side[d.price] = new
        self.seq = seq if seq is not None else self.seq
        self.receive_ts = receive_ts
        self.receive_mono = time.monotonic()
        self.exchange_ts = exchange_ts
        self.updates += 1
        if self.crossed():
            self.invalidate("crossed_book")
            return False
        return True

    def invalidate(self, reason: str) -> None:
        self.valid = False
        self.stale_reason = reason

    def age_ms(self, now_mono: float | None = None) -> float | None:
        if self.receive_mono is None:
            return None
        return ((now_mono if now_mono is not None else time.monotonic()) - self.receive_mono) * 1000.0

    # ---- quotes -----------------------------------------------------------------------------
    def best_yes_bid(self) -> Decimal | None:
        return max(self.yes) if (self.valid and self.yes) else None

    def best_no_bid(self) -> Decimal | None:
        return max(self.no) if (self.valid and self.no) else None

    def best_yes_ask(self) -> Decimal | None:
        nb = self.best_no_bid()
        return (ONE - nb) if nb is not None else None

    def best_no_ask(self) -> Decimal | None:
        yb = self.best_yes_bid()
        return (ONE - yb) if yb is not None else None

    def crossed(self) -> bool:
        yb = max(self.yes) if self.yes else None
        nb = max(self.no) if self.no else None
        # YES bid ≥ YES ask  ⇔  yb + nb ≥ 1  (would be an immediately matchable book)
        return yb is not None and nb is not None and yb + nb > ONE

    def mid_yes(self) -> Decimal | None:
        b, a = self.best_yes_bid(), self.best_yes_ask()
        if b is None or a is None:
            return None
        return (b + a) / 2

    def spread_yes(self) -> Decimal | None:
        b, a = self.best_yes_bid(), self.best_yes_ask()
        return None if b is None or a is None else a - b

    def bid_levels(self, side: str, n: int = 10) -> list[tuple[Decimal, Decimal]]:
        book = self.yes if side == "yes" else self.no
        return sorted(book.items(), key=lambda kv: -kv[0])[:n] if self.valid else []

    def ask_levels(self, side: str, n: int = 10) -> list[tuple[Decimal, Decimal]]:
        """Asks for ``side`` derived from the complementary bids, ascending price."""
        opp = self.no if side == "yes" else self.yes
        return sorted(((ONE - p, q) for p, q in opp.items()), key=lambda kv: kv[0])[:n] if self.valid else []

    # ---- executable prices for size (p.14, p.29) ---------------------------------------------
    def _walk(self, levels: list[tuple[Decimal, Decimal]], qty: Decimal, limit: Decimal | None, buy: bool) -> Fillable:
        remaining = Decimal(qty)
        cost = ZERO
        filled = ZERO
        used: list[tuple[Decimal, Decimal]] = []
        worst = None
        for px, avail in levels:
            if remaining <= 0:
                break
            if limit is not None and ((buy and px > limit) or (not buy and px < limit)):
                break
            take = min(avail, remaining)
            cost += take * px
            filled += take
            remaining -= take
            worst = px
            used.append((px, take))
        vwap = (cost / filled) if filled > 0 else None
        return Fillable(filled, cost, vwap, worst, tuple(used))

    def buy(self, side: str, qty: Decimal, limit: Decimal | None = None) -> Fillable:
        if not self.valid:
            return Fillable(ZERO, ZERO, None, None, ())
        opp = self.no if side == "yes" else self.yes
        levels = sorted(((ONE - p, q) for p, q in opp.items()), key=lambda kv: kv[0])
        return self._walk(levels, qty, limit, buy=True)

    def sell(self, side: str, qty: Decimal, limit: Decimal | None = None) -> Fillable:
        if not self.valid:
            return Fillable(ZERO, ZERO, None, None, ())
        book = self.yes if side == "yes" else self.no
        levels = sorted(book.items(), key=lambda kv: -kv[0])
        return self._walk(levels, qty, limit, buy=False)

    def depth_within(self, side: str, band: Decimal) -> tuple[Decimal, Decimal]:
        """(bid depth, ask depth) for ``side`` within ``band`` dollars of the touch."""
        bids = self.bid_levels(side, 100)
        asks = self.ask_levels(side, 100)
        if not bids or not asks:
            return ZERO, ZERO
        bb, ba = bids[0][0], asks[0][0]
        bd = sum((q for p, q in bids if bb - p <= band), ZERO)
        ad = sum((q for p, q in asks if p - ba <= band), ZERO)
        return bd, ad

    def microprice_yes(self) -> Decimal | None:
        bids = self.bid_levels("yes", 1)
        asks = self.ask_levels("yes", 1)
        if not bids or not asks:
            return None
        (b, bq), (a, aq) = bids[0], asks[0]
        if bq + aq == 0:
            return None
        return (a * bq + b * aq) / (bq + aq)

    def to_wire(self, n: int = 10) -> dict:
        return {
            "ticker": self.ticker,
            "valid": self.valid,
            "stale_reason": self.stale_reason,
            "seq": self.seq,
            "yes_bids": [[str(p), str(q)] for p, q in self.bid_levels("yes", n)],
            "no_bids": [[str(p), str(q)] for p, q in self.bid_levels("no", n)],
            "yes_asks": [[str(p), str(q)] for p, q in self.ask_levels("yes", n)],
            "no_asks": [[str(p), str(q)] for p, q in self.ask_levels("no", n)],
            "receive_ts": self.receive_ts,
        }
