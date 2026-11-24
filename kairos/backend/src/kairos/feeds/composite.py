"""Composite BTC/ETH/SOL proxy (spec p.3, p.46, p.132).

Median of synchronized, fresh venue midpoints. This is an **exchange proxy**, never the official CF
Benchmarks settlement index; consumers must label it as such. Basis versus the official reference is
estimated from settled outcomes (see models.settlement.BasisTracker).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .base import Quote


@dataclass(frozen=True)
class CompositePrice:
    asset: str
    value: float
    venues: tuple[str, ...]
    dispersion_bps: float
    oldest_age_ms: float
    outliers: tuple[str, ...]
    label: str = "exchange_proxy"


@dataclass
class Composite:
    max_age_ms: float = 5000.0
    outlier_bps: float = 25.0
    latest: dict[tuple[str, str], Quote] = field(default_factory=dict)

    def update(self, q: Quote) -> None:
        if q.bid <= 0 or q.ask < q.bid:
            return
        self.latest[(q.asset, q.venue)] = q

    def price(self, asset: str, now: float) -> CompositePrice | None:
        fresh = [q for (a, _), q in self.latest.items() if a == asset and (now - q.receive_ts) * 1000 <= self.max_age_ms]
        if not fresh:
            return None
        mids = [q.mid for q in fresh]
        med = statistics.median(mids)
        disp = (max(mids) - min(mids)) / med * 1e4 if med > 0 else 0.0
        outliers = tuple(sorted(q.venue for q in fresh if abs(q.mid - med) / med * 1e4 > self.outlier_bps)) if len(fresh) >= 3 else ()
        if outliers:
            kept = [q.mid for q in fresh if q.venue not in outliers]
            med = statistics.median(kept) if kept else med
        oldest = max((now - q.receive_ts) * 1000 for q in fresh)
        return CompositePrice(asset, med, tuple(sorted(q.venue for q in fresh)), disp, oldest, outliers)
