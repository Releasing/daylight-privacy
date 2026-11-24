"""Autonomous operating mode controller (spec p.67, p.74).

mode = min(health, market, execution, validated_strategy, policy_max); any hard veto → PAUSED.
Downgrades apply immediately. Upgrades require a dwell period and several independent observations and
never happen because of a loss or a desire to trade more.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal

from ..config import ModeCeilings


class Mode(enum.IntEnum):
    PAUSED = 0
    DEFENSIVE = 1
    CONSERVATIVE = 2
    STANDARD = 3
    OPPORTUNISTIC = 4
    AGGRESSIVE = 5

    def ceiling(self, c: ModeCeilings) -> Decimal:
        return getattr(c, self.name.lower())


@dataclass
class ModeController:
    dwell_s: float = 900.0
    required_obs: int = 3
    aggressive_enabled: bool = False
    current: Mode = Mode.PAUSED  # restart in halted/paused state until reconciliation (p.18)
    since: float = 0.0
    pending_up: Mode | None = None
    pending_obs: int = 0
    transitions: list[tuple[float, str, str, list[str]]] = field(default_factory=list)

    def evaluate(self, now: float, health: Mode, market: Mode, execution: Mode, strategy: Mode, policy_max: Mode,
                 hard_vetoes: list[str]) -> Mode:
        cap = policy_max if self.aggressive_enabled else min(policy_max, Mode.OPPORTUNISTIC)
        target = min(health, market, execution, strategy, cap)
        reasons = list(hard_vetoes)
        if hard_vetoes:
            target = Mode.PAUSED
        if target < self.current:
            self._set(now, target, reasons or ["downgrade"])
            self.pending_up, self.pending_obs = None, 0
        elif target > self.current:
            if self.pending_up != target:
                self.pending_up, self.pending_obs = target, 1
            else:
                self.pending_obs += 1
            # leaving PAUSED after cleared vetoes requires only the observation count (reconciliation is gated elsewhere)
            dwell_ok = (now - self.since) >= self.dwell_s or self.current == Mode.PAUSED
            if dwell_ok and self.pending_obs >= self.required_obs:
                # step up one level at a time to avoid oscillatory escalation
                self._set(now, Mode(self.current + 1), ["sustained_evidence"])
                self.pending_up, self.pending_obs = None, 0
        else:
            self.pending_up, self.pending_obs = None, 0
        return self.current

    def _set(self, now: float, m: Mode, reasons: list[str]) -> None:
        self.transitions.append((now, self.current.name, m.name, reasons))
        self.current, self.since = m, now
