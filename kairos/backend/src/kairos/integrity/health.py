"""Feed health and market-integrity checks (spec p.3, p.46–47, p.105, p.107).

Produces reason codes. Critical failures (Kalshi book, BTC proxy) block new entries; auxiliary
failures (ETH/SOL) only switch prediction to the BTC-only fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..feeds.composite import Composite
from ..market.book import OrderBook


@dataclass
class HealthReport:
    entry_blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    btc_only_fallback: bool = False
    feed_age_ms: dict[str, float | None] = field(default_factory=dict)

    @property
    def entries_allowed(self) -> bool:
        return not self.entry_blockers


@dataclass
class LiquidityIntegrity:
    """Displayed depth persistence and cancellation intensity (p.46–47). Cancellations alone are not
    proof of spoofing; the output only reduces confidence."""

    window: int = 50
    history: list[tuple[Decimal, Decimal]] = field(default_factory=list)

    def observe(self, book: OrderBook) -> None:
        self.history.append((book.added_qty, book.removed_qty))
        if len(self.history) > self.window:
            self.history.pop(0)

    def cancel_to_add(self) -> float | None:
        if len(self.history) < 2:
            return None
        a0, r0 = self.history[0]
        a1, r1 = self.history[-1]
        added, removed = float(a1 - a0), float(r1 - r0)
        if added <= 0:
            return None
        return removed / added

    def confidence_penalty(self) -> float:
        r = self.cancel_to_add()
        if r is None:
            return 0.0
        return max(0.0, min(1.0, (r - 1.0) / 2.0))  # 0 when ratio ≤ 1, saturates at ratio 3


def evaluate(
    book: OrderBook | None,
    composite: Composite,
    now: float,
    max_quote_age_ms: float,
    max_feed_age_ms: float,
    clock_skew_ms: float | None = None,
    max_clock_skew_ms: float = 1000.0,
    exchange_open: bool | None = True,
) -> HealthReport:
    rep = HealthReport()
    if exchange_open is False:
        rep.entry_blockers.append("exchange_closed_or_maintenance")
    if book is None:
        rep.entry_blockers.append("no_contract_book")
    else:
        if not book.valid:
            rep.entry_blockers.append(f"book_invalid:{book.stale_reason}")
        age = book.age_ms()
        rep.feed_age_ms["kalshi_book"] = age
        if age is None or age > max_quote_age_ms:
            rep.entry_blockers.append("book_stale")
    btc = composite.price("BTC", now)
    rep.feed_age_ms["btc_proxy"] = btc.oldest_age_ms if btc else None
    if btc is None or btc.oldest_age_ms > max_feed_age_ms:
        rep.entry_blockers.append("btc_proxy_stale")
    elif btc.outliers:
        rep.warnings.append("venue_outlier:" + ",".join(btc.outliers))
    for aux in ("ETH", "SOL"):
        p = composite.price(aux, now)
        rep.feed_age_ms[f"{aux.lower()}_proxy"] = p.oldest_age_ms if p else None
        if p is None:
            rep.btc_only_fallback = True
            rep.warnings.append(f"{aux.lower()}_unavailable_fallback")
    if clock_skew_ms is not None and abs(clock_skew_ms) > max_clock_skew_ms:
        rep.entry_blockers.append("clock_skew")
    return rep
