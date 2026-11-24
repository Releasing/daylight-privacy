"""Profit protection, adaptive distribution and high-water-mark accounting (spec p.68–72, p.76).

Buckets: active trading capital, protected savings, restricted reserve. Only active capital is eligible
for orders. Eligible profit is recognized only from realized, reconciled P&L above the high-water mark:

    R = cumulative reconciled realized net P&L
    new_profit = max(0, R − H);  H_next = max(H, R)

Distribution uses integer cents; any rounding residual goes to savings. Each event ID is applied exactly
once. Policy changes affect future profit only; buckets never go negative and savings are never
reclassified to fund trading. Deposits/withdrawals are capital flows, not profit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from ..money import CENT, ZERO

POLICIES: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "healthy_growth": (Decimal("0.70"), Decimal("0.25"), Decimal("0.05")),
    "balanced": (Decimal("0.50"), Decimal("0.40"), Decimal("0.10")),
    "elevated_risk": (Decimal("0.30"), Decimal("0.60"), Decimal("0.10")),
    "capital_preservation": (Decimal("0.10"), Decimal("0.80"), Decimal("0.10")),
}
ORDER = ["capital_preservation", "elevated_risk", "balanced", "healthy_growth"]


class FinanceError(RuntimeError):
    pass


@dataclass
class Buckets:
    active: Decimal = ZERO
    savings: Decimal = ZERO
    reserve: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.active + self.savings + self.reserve


@dataclass
class FundsAccount:
    buckets: Buckets = field(default_factory=Buckets)
    cumulative_realized: Decimal = ZERO  # R
    high_water: Decimal = ZERO  # H
    policy: str = "balanced"
    frozen: bool = False  # corrections freeze distribution until reconciled
    applied_events: set[str] = field(default_factory=set)
    journal: list[dict] = field(default_factory=list)
    pending_withdrawal: Decimal = ZERO

    # ---- capital flows ----------------------------------------------------------------------
    def deposit(self, event_id: str, amount: Decimal, ts: float) -> None:
        if event_id in self.applied_events:
            return
        if amount <= 0:
            raise FinanceError("deposit must be positive")
        self.applied_events.add(event_id)
        self.buckets.active += amount
        self.journal.append({"event_id": event_id, "type": "deposit", "ts": ts, "active": str(amount)})

    def request_withdrawal(self, amount: Decimal) -> None:
        """Administrative only; recorded as pending (exchange API transfer support is not assumed)."""
        if amount <= 0 or amount > self.buckets.savings + self.buckets.active:
            raise FinanceError("invalid withdrawal")
        self.pending_withdrawal += amount

    # ---- realized P&L -------------------------------------------------------------------------
    def apply_realized(self, event_id: str, realized: Decimal, ts: float) -> dict[str, Decimal]:
        """Apply one confirmed realized P&L event atomically and exactly once."""
        if event_id in self.applied_events:
            return {}
        self.applied_events.add(event_id)
        self.cumulative_realized += realized
        self.buckets.active += realized  # gains/losses settle in active cash first
        credits = {"active": ZERO, "savings": ZERO, "reserve": ZERO}
        if not self.frozen:
            new_profit = max(ZERO, self.cumulative_realized - self.high_water)
            self.high_water = max(self.high_water, self.cumulative_realized)
            if new_profit > 0:
                credits = self._distribute(new_profit)
        if self.buckets.active < 0:
            # losses beyond active capital cannot be covered by savings (never borrow from protected buckets)
            self.journal.append({"event_id": event_id, "type": "active_deficit", "ts": ts, "deficit": str(self.buckets.active)})
        self.journal.append({"event_id": event_id, "type": "realized", "ts": ts, "realized": str(realized),
                             "R": str(self.cumulative_realized), "H": str(self.high_water), "policy": self.policy,
                             **{k: str(v) for k, v in credits.items()}})
        return credits

    def _distribute(self, profit: Decimal) -> dict[str, Decimal]:
        a, s, r = POLICIES[self.policy]
        cents = profit.quantize(CENT, rounding=ROUND_DOWN)
        to_res = (cents * r).quantize(CENT, rounding=ROUND_DOWN)
        to_sav_base = (cents * s).quantize(CENT, rounding=ROUND_DOWN)
        to_act = (cents * a).quantize(CENT, rounding=ROUND_DOWN)
        residual = profit - to_res - to_sav_base - to_act  # rounding residual → savings (p.69)
        to_sav = to_sav_base + residual
        # profit already sits in active; move the protected portions out
        self.buckets.active -= (to_sav + to_res)
        self.buckets.savings += to_sav
        self.buckets.reserve += to_res
        return {"active": to_act, "savings": to_sav, "reserve": to_res}

    def correction(self, event_id: str, delta: Decimal, ts: float) -> None:
        """Exchange correction to prior realized P&L: freeze distributions until reconciled (p.70)."""
        if event_id in self.applied_events:
            return
        self.applied_events.add(event_id)
        self.frozen = True
        self.cumulative_realized += delta
        self.buckets.active += delta
        self.journal.append({"event_id": event_id, "type": "correction", "ts": ts, "delta": str(delta)})

    def unfreeze_after_reconcile(self) -> None:
        self.frozen = False

    # ---- adaptive policy (p.69) -----------------------------------------------------------------
    def select_policy(self, rolling_net_ev: float | None, drawdown: float, execution_ok: bool, integrity_ok: bool,
                      sustained_recovery: bool) -> str:
        if not integrity_ok or drawdown >= 0.08:
            target = "capital_preservation"
        elif not execution_ok or drawdown >= 0.04 or (rolling_net_ev is not None and rolling_net_ev < 0):
            target = "elevated_risk"
        elif rolling_net_ev is not None and rolling_net_ev > 0 and drawdown < 0.02:
            target = "healthy_growth"
        else:
            target = "balanced"
        cur, tgt = ORDER.index(self.policy), ORDER.index(target)
        if tgt < cur:
            self.policy = target  # deteriorating health moves toward preservation immediately
        elif tgt > cur and sustained_recovery:
            self.policy = ORDER[cur + 1]  # upgrade one step only after sustained recovery
        return self.policy

    # ---- spendable cash (p.68) ------------------------------------------------------------------
    def spendable(self, reconciled_cash: Decimal, open_order_commitments: Decimal, fee_buffer: Decimal = ZERO,
                  exchange_reserved_included: bool = False) -> Decimal:
        protected = self.buckets.savings + self.buckets.reserve + self.pending_withdrawal
        commit = ZERO if exchange_reserved_included else open_order_commitments
        return max(ZERO, min(self.buckets.active, reconciled_cash - protected) - commit - fee_buffer)
