"""Trade economics and Kelly sizing (spec p.10, p.14, p.59, p.61, p.79–80, p.103)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from ..market.book import OrderBook
from ..money import ZERO, FeeSchedule, D


@dataclass(frozen=True)
class EntryEconomics:
    side: str
    qty: Decimal
    vwap: Decimal | None
    cost: Decimal  # contracts × price
    fee: Decimal
    all_in: Decimal  # cost + fee (cash reserved)
    ev: Decimal  # expected net dollars at point probability
    ev_conservative: Decimal  # at conservative probability
    max_loss: Decimal  # all_in (binary can expire worthless)
    max_payout: Decimal  # qty × $1

    @property
    def ev_per_dollar(self) -> float:
        return float(self.ev_conservative / self.all_in) if self.all_in > 0 else 0.0


def settlement_entry(book: OrderBook, side: str, qty: Decimal, p_side: float, p_side_conservative: float,
                     fees: FeeSchedule, limit: Decimal | None = None) -> EntryEconomics | None:
    """EV = q(p − c) − entry fees (p.79). ``p_side`` is the probability that ``side`` settles in the money."""
    f = book.buy(side, qty, limit)
    if f.quantity < qty or f.vwap is None:
        return None  # insufficient executable depth — never assume more liquidity
    fee = fees.fee(qty, f.vwap)
    all_in = f.cost + fee
    ev = qty * D(round(p_side, 6)) - all_in
    evc = qty * D(round(p_side_conservative, 6)) - all_in
    return EntryEconomics(side, qty, f.vwap, f.cost, fee, all_in, ev, evc, all_in, qty)


def per_contract_ev(p: float, ask: float, cost_per_contract: float) -> float:
    """Per-contract net EV = p − ask − costs (p.103 example 0.65 − 0.50 − 0.03 = 0.12)."""
    return p - ask - cost_per_contract


def kelly_binary(p: float, cost: float, fee_per_contract: float = 0.0) -> float:
    """Fractional-Kelly building block (p.61): f = max(0, (p·b − (1−p)·a)/(a·b)) with net win b and loss a
    per dollar risked. Without fees f = max(0,(p−c)/(1−c)). Invalid inputs → 0."""
    if not all(math.isfinite(v) for v in (p, cost, fee_per_contract)):
        return 0.0
    if not (0.0 <= p <= 1.0) or not (0.0 < cost < 1.0) or fee_per_contract < 0:
        return 0.0
    stake = cost + fee_per_contract
    if stake >= 1.0:
        return 0.0
    b = (1.0 - stake) / stake
    a = 1.0
    return max(0.0, (p * b - (1 - p) * a) / (a * b))


def kelly_scenarios(probs: list[float], returns: list[float], f_max: float = 1.0, steps: int = 400) -> float:
    """Maximize Σ P_j log(1 + f r_j) over f ∈ [0, f_max] keeping wealth positive in every scenario (p.61)."""
    if not probs or len(probs) != len(returns):
        return 0.0
    if any((not math.isfinite(x)) for x in probs + returns) or abs(sum(probs) - 1.0) > 1e-6 or min(probs) < 0:
        return 0.0
    worst = min(returns)
    cap = f_max if worst >= 0 else min(f_max, 0.999 / -worst)
    best_f, best_g = 0.0, 0.0
    for i in range(1, steps + 1):
        f = cap * i / steps
        g = sum(p * math.log(1 + f * r) for p, r in zip(probs, returns) if p > 0)
        if g > best_g:
            best_f, best_g = f, g
    return best_f


def largest_feasible_quantity(budget: Decimal, book: OrderBook, side: str, fees: FeeSchedule,
                              max_qty: int = 10000, limit: Decimal | None = None) -> tuple[int, Decimal]:
    """Largest integer q with all-in cost(q) ≤ budget given visible depth. Never rounds up (p.60, p.63)."""
    lo, hi = 0, max_qty
    best_cost = ZERO
    while lo < hi:
        mid = (lo + hi + 1) // 2
        f = book.buy(side, Decimal(mid), limit)
        if f.quantity < mid or f.vwap is None:
            hi = mid - 1
            continue
        total = f.cost + fees.fee(mid, f.vwap)
        if total <= budget:
            lo, best_cost = mid, total
        else:
            hi = mid - 1
    if lo > 0 and best_cost == ZERO:
        f = book.buy(side, Decimal(lo), limit)
        best_cost = f.cost + fees.fee(lo, f.vwap)  # type: ignore[arg-type]
    return lo, best_cost
