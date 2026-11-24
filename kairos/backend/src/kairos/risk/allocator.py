"""Atomic capital allocator and independent risk gate (spec p.16, p.21, p.35, p.52, p.60, p.63, p.71–72,
p.74, p.107, p.135).

One allocator per account is shared by every strategy, both contract sides and manual orders. Cash and
worst-case loss are reserved atomically before submission and released only on authoritative terminal
state. Nothing here can be modified by research, strategy or AI code (the policy is frozen).
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from decimal import Decimal

from ..config import RiskPolicy
from ..contracts import Candidate
from ..market.book import OrderBook
from ..money import ZERO, FeeSchedule, D
from ..strategy.economics import largest_feasible_quantity
from .mode import Mode


@dataclass
class Reservation:
    key: str
    ticker: str
    cash: Decimal  # all-in cost reserved (price × qty + fees)
    worst_loss: Decimal
    origin: str  # bot | manual
    strategy: str


@dataclass
class AccountState:
    """Reconciled financial state used by the gate (amounts in dollars)."""

    active_equity: Decimal  # active trading capital (excludes protected savings + reserve)
    spendable_cash: Decimal  # reconciled cash − protected − reserve − open-order commitments
    realized_pnl_today: Decimal = ZERO
    peak_equity: Decimal = ZERO
    open_exposure_by_market: dict[str, Decimal] = field(default_factory=dict)  # worst-case loss of inventory
    open_positions: int = 0
    open_orders: int = 0
    orders_by_market: dict[str, int] = field(default_factory=dict)
    turnover_by_market: dict[str, Decimal] = field(default_factory=dict)
    reconciled: bool = False
    consecutive_rejects: int = 0


@dataclass
class Approval:
    approved: bool
    qty: int
    budget: Decimal
    all_in: Decimal
    caps: dict[str, str]
    reasons: list[str]

    def to_wire(self) -> dict:
        return {"approved": self.approved, "qty": self.qty, "budget": str(self.budget), "all_in": str(self.all_in),
                "caps": self.caps, "reasons": self.reasons}


class RiskGate:
    def __init__(self, policy: RiskPolicy, paper: bool = True):
        self.policy = policy
        self.paper = paper
        self._lock = threading.Lock()
        self.reservations: dict[str, Reservation] = {}

    # ---- reservations ------------------------------------------------------------------------
    def reserved_cash(self) -> Decimal:
        return sum((r.cash for r in self.reservations.values()), ZERO)

    def reserved_loss(self, ticker: str | None = None) -> Decimal:
        return sum((r.worst_loss for r in self.reservations.values() if ticker is None or r.ticker == ticker), ZERO)

    def release(self, key: str, filled_cash: Decimal = ZERO) -> Decimal:
        """Release a reservation on authoritative terminal state; returns released excess."""
        with self._lock:
            r = self.reservations.pop(key, None)
            if r is None:
                return ZERO
            return max(ZERO, r.cash - filled_cash)

    def shrink(self, key: str, filled_cash: Decimal, filled_loss: Decimal) -> None:
        """A partial fill converts part of the reservation into inventory (counted in account state)."""
        with self._lock:
            r = self.reservations.get(key)
            if r is not None:
                r.cash = max(ZERO, r.cash - filled_cash)
                r.worst_loss = max(ZERO, r.worst_loss - filled_loss)

    # ---- hard checks --------------------------------------------------------------------------
    def hard_checks(self, c: Candidate, acct: AccountState, mode: Mode, halts: list[str], health_blockers: list[str],
                    series_ok: bool, market_reasons: list[str], seconds_left: float, quote_age_ms: float | None,
                    origin: str = "bot") -> list[str]:
        p = self.policy
        r: list[str] = []
        if not series_ok:
            r.append("market_not_allowlisted")
        r.extend(f"halt:{h}" for h in halts)
        r.extend(health_blockers)
        r.extend(market_reasons)
        if not acct.reconciled:
            r.append("account_unreconciled")
        if origin == "bot" and mode == Mode.PAUSED:
            r.append("mode_paused")
        if quote_age_ms is None or quote_age_ms > p.max_quote_age_ms:
            r.append("quote_stale")
        if c.action == "buy" and origin == "bot" and c.strategy_id != "settlement" and seconds_left < p.entry_cutoff_seconds:
            r.append("after_entry_cutoff")
        if seconds_left <= 0:
            r.append("market_closed")
        if acct.open_orders >= p.max_open_orders:
            r.append("max_open_orders")
        if acct.orders_by_market.get(c.ticker, 0) >= p.max_orders_per_market:
            r.append("churn_order_budget")
        if acct.open_positions >= p.max_concurrent_positions and c.ticker not in acct.open_exposure_by_market:
            r.append("max_concurrent_positions")
        if acct.consecutive_rejects >= p.max_rejects_before_halt:
            r.append("repeated_rejects")
        if origin == "bot":
            if c.ev_per_contract is None or not math.isfinite(float(c.ev_per_contract)) or c.ev_per_contract <= 0:
                r.append("nonpositive_conservative_ev")
            if not (0.0 <= c.p_conservative <= 1.0):
                r.append("invalid_probability")
            if c.confidence is None:
                r.append("confidence_missing_inputs")
            elif c.confidence < p.confidence.reject_below:
                r.append("confidence_below_reject")
        # daily loss / drawdown
        loss_limit = acct.active_equity * p.daily_loss_fraction
        if -acct.realized_pnl_today >= loss_limit:
            r.append("daily_loss_limit")
        if acct.peak_equity > 0 and (acct.peak_equity - acct.active_equity) >= acct.peak_equity * p.max_drawdown_fraction:
            r.append("drawdown_limit")
        return sorted(set(r))

    # ---- sizing (p.63, p.71) ---------------------------------------------------------------------
    def approve(self, c: Candidate, acct: AccountState, mode: Mode, book: OrderBook, fees: FeeSchedule,
                hard_reasons: list[str], key: str, origin: str = "bot", requested_qty: int | None = None) -> Approval:
        p = self.policy
        if hard_reasons:
            return Approval(False, 0, ZERO, ZERO, {}, hard_reasons)
        with self._lock:
            E = acct.active_equity
            caps: dict[str, Decimal] = {}
            if origin == "bot":
                caps["mode"] = E * mode.ceiling(p.mode_ceilings)
                tier = p.confidence.cap_for(c.confidence or 0)
                if tier == 0 and self.paper and (c.confidence or 0) >= p.confidence.reject_below:
                    tier = p.mode_ceilings.defensive  # observe band: paper-only minimal research size (p.52)
                caps["confidence"] = E * tier
                kf = D(round(max(0.0, c.kelly_fraction), 6)) * p.kelly_fraction
                caps["kelly"] = E * kf
                caps["entry_ceiling"] = E * p.max_entry_fraction
            free_cash = acct.spendable_cash - self.reserved_cash()
            caps["cash"] = free_cash
            mkt_used = acct.open_exposure_by_market.get(c.ticker, ZERO) + self.reserved_loss(c.ticker)
            caps["market_headroom"] = E * p.max_market_exposure_fraction - mkt_used
            port_used = sum(acct.open_exposure_by_market.values(), ZERO) + self.reserved_loss()
            caps["portfolio_headroom"] = E * p.max_portfolio_exposure_fraction - port_used
            loss_room = E * p.daily_loss_fraction + min(acct.realized_pnl_today, ZERO) - port_used
            caps["loss_headroom"] = loss_room if origin == "bot" else caps["cash"]
            turnover = acct.turnover_by_market.get(c.ticker, ZERO)
            caps["turnover"] = E * p.max_turnover_per_market_fraction - turnover
            budget = max(ZERO, min(caps.values()))
            reasons: list[str] = []
            binding = min(caps, key=lambda k: caps[k])
            max_q = min(c.max_qty, requested_qty) if requested_qty else c.max_qty
            qty, all_in = largest_feasible_quantity(budget, book, c.side, fees, max_qty=max_q, limit=c.limit_price)
            if qty < p.min_contracts:
                reasons.append(f"size_below_minimum:{binding}")
                return Approval(False, 0, budget, ZERO, {k: str(v) for k, v in caps.items()}, reasons)
            if origin == "manual" and requested_qty and qty < requested_qty:
                reasons.append(f"manual_qty_reduced:{binding}")
                return Approval(False, qty, budget, all_in, {k: str(v) for k, v in caps.items()}, reasons)
            self.reservations[key] = Reservation(key, c.ticker, all_in, all_in, origin, c.strategy_id)
            return Approval(True, qty, budget, all_in, {k: str(v) for k, v in caps.items()}, [f"binding:{binding}"])
