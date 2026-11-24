"""Deterministic replay, scalp path labels and counterfactuals (spec p.5–6, p.24, p.92, p.108).

* Events are replayed strictly by **receipt time**; a decision can only use events already received.
* Replaying identical events + configuration twice must yield identical decisions and ledger (hash).
* Path labels measure first passage of the *executable bid* to target vs stop vs timeout, quantity-aware.
  A midpoint touch is never a fill.
* Counterfactual exits (±1/3/5/10 s) use only recorded executable bids. The future-best exit is reported
  as an oracle diagnostic only.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from ..money import FeeSchedule


@dataclass(frozen=True)
class BidPoint:
    t: float  # receipt time
    bid: Decimal | None  # executable bid for the side held
    depth: Decimal  # size at that bid


@dataclass(frozen=True)
class PathLabel:
    outcome: str  # target | stop | timeout | unfilled_exit
    exit_t: float | None
    exit_price: Decimal | None
    filled_qty: Decimal
    remaining_qty: Decimal
    max_adverse: Decimal


def path_label(entry_price: Decimal, entry_t: float, qty: Decimal, path: list[BidPoint], target: Decimal, stop: Decimal,
               cutoff_t: float, latency_s: float = 0.25) -> PathLabel:
    """Label one candidate entry. Exits act ``latency_s`` after the triggering observation, at the bid then
    observed, limited to visible depth; the remainder is reported as residual exposure."""
    worst = Decimal(0)
    pts = sorted((p for p in path if p.t >= entry_t), key=lambda p: p.t)
    for i, p in enumerate(pts):
        if p.bid is not None:
            worst = min(worst, p.bid - entry_price)
        if p.t > cutoff_t:
            break
        if p.bid is None:
            continue
        hit = "target" if p.bid >= entry_price + target else ("stop" if p.bid <= entry_price - stop else None)
        if hit:
            later = next((q for q in pts[i:] if q.t >= p.t + latency_s and q.bid is not None), None)
            if later is None:
                return PathLabel("unfilled_exit", None, None, Decimal(0), qty, worst)
            fq = min(qty, later.depth)
            return PathLabel(hit, later.t, later.bid, fq, qty - fq, worst)
    last = next((q for q in reversed(pts) if q.t <= cutoff_t and q.bid is not None), None)
    if last is None:
        return PathLabel("unfilled_exit", None, None, Decimal(0), qty, worst)
    fq = min(qty, last.depth)
    return PathLabel("timeout", last.t, last.bid, fq, qty - fq, worst)


def counterfactual_exits(entry_price: Decimal, qty: int, actual_exit_t: float, path: list[BidPoint], fees: FeeSchedule,
                         settle_value: Decimal | None, offsets: Iterable[int] = (-10, -5, -3, -1, 1, 3, 5, 10)) -> dict[str, Any]:
    def net_at(t: float) -> Decimal | None:
        p = next((q for q in sorted(path, key=lambda x: x.t) if q.t >= t and q.bid is not None), None)
        if p is None:
            return None
        fq = min(Decimal(qty), p.depth)
        proceeds = fq * p.bid - (fees.fee(fq, p.bid) if fq > 0 else 0)
        resid = (Decimal(qty) - fq) * (settle_value if settle_value is not None else Decimal(0))
        return proceeds + resid - Decimal(qty) * entry_price
    out: dict[str, Any] = {f"{o:+d}s": net_at(actual_exit_t + o) for o in offsets}
    if settle_value is not None:
        out["hold_to_settlement"] = Decimal(qty) * (settle_value - entry_price)
    best = max((p for p in path if p.bid is not None), key=lambda p: p.bid, default=None)
    out["oracle_best_exit_diagnostic_only"] = None if best is None else Decimal(qty) * (best.bid - entry_price)
    return out


def ledger_hash(ledger) -> str:
    rows = [(r.ticker, r.side, str(r.qty), str(r.proceeds), str(r.exit_fee), str(r.realized), r.kind) for r in ledger.realizations]
    pos = sorted((k[0], k[1], str(p.qty), str(p.cost)) for k, p in ledger.positions.items())
    return hashlib.sha256(json.dumps([rows, pos]).encode()).hexdigest()


def decisions_hash(decisions: Iterable[dict]) -> str:
    keep = [(d.get("ticker"), d.get("strategy_id"), d.get("side"), d.get("action"), d.get("qty"), str(d.get("price")),
             tuple(d.get("reason_codes") or [])) for d in decisions]
    return hashlib.sha256(json.dumps(keep, default=str).encode()).hexdigest()


async def replay_events(events: list[dict], market, account, view_at) -> dict:
    """Feed recorded events (sorted by receive_ts) into a MarketService and evaluate the account at each
    event's receipt time. ``view_at(t)`` builds the MarketView as of t (no look-ahead)."""
    from ..feeds.base import Quote, Trade

    for ev in sorted(events, key=lambda e: (e["receive_ts"], e.get("sequence") or 0)):
        t = ev["receive_ts"]
        kind = ev["kind"]
        if kind == "quote":
            await market.on_quote(Quote(ev["venue"], ev["asset"], ev["bid"], ev["ask"], ev.get("exchange_ts"), t))
        elif kind == "trade":
            await market.on_trade(Trade(ev["venue"], ev["asset"], ev["price"], ev["size"], ev.get("side"), ev.get("exchange_ts"), t))
        elif kind == "kalshi":
            await market.on_ws_message(ev["msg"], t, 0)
        elif kind == "settlement":
            await account.on_settlement(ev["ticker"], ev["result"])
        if ev.get("evaluate"):
            await account.evaluate(view_at(t))
    return {"ledger": ledger_hash(account.ledger), "decisions": decisions_hash(list(account.decisions))}
