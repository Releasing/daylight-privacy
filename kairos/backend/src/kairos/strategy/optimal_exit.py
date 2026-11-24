"""Optimal stopping and dynamic exits (spec p.12, p.22, p.85–86).

For each feasible action (sell all, sell part, hold) compare net proceeds now plus the risk-adjusted
continuation value of the residual. Entry cost is sunk for this comparison. Risk requirements override.
A conservative fallback is used when the model output is stale.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..market.book import OrderBook
from ..money import ZERO, FeeSchedule, D


@dataclass(frozen=True)
class ExitDecision:
    action: str  # hold | sell_part | sell_all
    qty: int
    limit_price: Decimal | None
    value_now: Decimal
    value_hold: Decimal
    reason: str


def evaluate_exit(book: OrderBook, side: str, qty: int, p_side_conservative: float | None, fees: FeeSchedule,
                  tail_penalty: float = 0.02, min_improvement: Decimal = Decimal("0.01"), allow_partial: bool = True,
                  risk_requires_exit: bool = False, model_stale: bool = False, scalp_target: Decimal | None = None,
                  scalp_stop: Decimal | None = None, past_cutoff: bool = False) -> ExitDecision:
    if qty <= 0:
        return ExitDecision("hold", 0, None, ZERO, ZERO, "flat")

    def proceeds(q: int) -> tuple[Decimal, Decimal | None, int]:
        f = book.sell(side, Decimal(q))
        if f.quantity <= 0 or f.vwap is None:
            return ZERO, None, 0
        fq = int(f.quantity)
        return f.cost - fees.fee(fq, f.vwap), f.worst, fq

    all_val, all_px, all_q = proceeds(qty)
    if risk_requires_exit:
        if all_q == 0:
            return ExitDecision("hold", 0, None, ZERO, ZERO, "risk_exit_no_bids")  # cannot assume liquidation
        return ExitDecision("sell_all", all_q, all_px, all_val, ZERO, "risk_required_exit")
    best_bid = book.best_yes_bid() if side == "yes" else book.best_no_bid()
    # scalp policy: predeclared target / stop / time cutoff on executable bid
    if scalp_target is not None and best_bid is not None and best_bid >= scalp_target and all_q:
        return ExitDecision("sell_all", all_q, all_px, all_val, ZERO, "scalp_target_bid")
    if scalp_stop is not None and best_bid is not None and best_bid <= scalp_stop and all_q:
        return ExitDecision("sell_all", all_q, all_px, all_val, ZERO, "scalp_stop_bid")
    if past_cutoff and scalp_target is not None and all_q:
        return ExitDecision("sell_all", all_q, all_px, all_val, ZERO, "scalp_timeout")
    if model_stale or p_side_conservative is None:
        # conservative fallback: hold unless bids exceed model-free floor (do nothing hasty, no new risk)
        return ExitDecision("hold", 0, None, all_val, ZERO, "model_stale_fallback_hold")
    per = D(round(max(0.0, p_side_conservative - tail_penalty), 6))
    hold_all = per * qty
    best = ExitDecision("hold", 0, None, ZERO, hold_all, "continuation_value_higher")
    best_total = hold_all
    candidates = [qty] + ([q for q in (qty * 3 // 4, qty // 2, qty // 4) if 0 < q < qty] if allow_partial else [])
    for q in candidates:
        v, px, fq = proceeds(q)
        if fq == 0:
            continue
        total = v + per * (qty - fq)
        if total > best_total + min_improvement:
            best_total = total
            best = ExitDecision("sell_all" if fq == qty else "sell_part", fq, px, v, per * (qty - fq), "liquidation_beats_continuation")
    return best
