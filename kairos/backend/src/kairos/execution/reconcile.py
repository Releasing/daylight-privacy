"""Reconciliation and post-fill markouts (spec p.12, p.15, p.48)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from ..ledger.lots import Ledger
from ..money import ZERO


@dataclass
class ReconcileResult:
    ok: bool
    mismatches: list[str] = field(default_factory=list)


def reconcile_positions(ledger: Ledger, exchange_positions: dict[tuple[str, str], Decimal]) -> ReconcileResult:
    """Compare ledger inventory with authoritative exchange positions; any mismatch → halt new orders."""
    mism = []
    keys = set(exchange_positions) | {(p.ticker, p.side) for p in ledger.open_positions()}
    for k in sorted(keys):
        lq = ledger.positions[k].qty if k in ledger.positions else ZERO
        eq = exchange_positions.get(k, ZERO)
        if lq != eq:
            mism.append(f"{k[0]}/{k[1]}: ledger={lq} exchange={eq}")
    return ReconcileResult(not mism, mism)


def reconcile_cash(expected: Decimal, exchange_cash: Decimal, tolerance: Decimal = Decimal("0.01")) -> ReconcileResult:
    diff = exchange_cash - expected
    if abs(diff) > tolerance:
        return ReconcileResult(False, [f"cash diff {diff}"])
    return ReconcileResult(True)


@dataclass
class Markouts:
    """Signed markouts at 1/5/30 s after fills: (mid_later − fill price)·direction (p.48)."""

    horizons: tuple[int, ...] = (1, 5, 30)
    pending: list[dict] = field(default_factory=list)
    results: dict[str, dict[int, list[float]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))

    def on_fill(self, strategy: str, side: str, action: str, price: float, ts: float, maker: bool) -> None:
        self.pending.append({"strategy": f"{strategy}:{'maker' if maker else 'taker'}", "side": side,
                             "dir": 1.0 if action == "buy" else -1.0, "price": price, "ts": ts, "done": set()})

    def on_mid(self, side_mid: dict[str, float | None], now: float) -> None:
        keep = []
        for p in self.pending:
            mid = side_mid.get(p["side"])
            for h in self.horizons:
                if h not in p["done"] and now - p["ts"] >= h and mid is not None:
                    self.results[p["strategy"]][h].append(p["dir"] * (mid - p["price"]))
                    p["done"].add(h)
            if len(p["done"]) < len(self.horizons) and now - p["ts"] < max(self.horizons) + 30:
                keep.append(p)
        self.pending = keep

    def summary(self) -> dict:
        return {s: {h: (sum(v) / len(v) if v else None) for h, v in hs.items()} for s, hs in self.results.items()}

    def deteriorating(self, strategy: str, min_n: int = 30, threshold: float = -0.02) -> bool:
        v = self.results.get(strategy, {}).get(5, [])
        return len(v) >= min_n and sum(v[-min_n:]) / min_n < threshold
