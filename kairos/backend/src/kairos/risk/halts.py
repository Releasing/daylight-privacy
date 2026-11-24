"""Halt triggers and recovery policy (spec p.16, p.41, p.74, p.115).

* auto-recoverable: stale feeds, sequence gaps, clock drift, abnormal latency, exchange maintenance —
  clear after verified snapshot + reconciliation + cooldown.
* daily lock: daily loss limit — persists until the configured UTC reset.
* latched: security incident, ledger/settlement mismatch, storage write failure, repeated rejects,
  unexpected rules/fee change — require administrative acknowledgement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

AUTO = {"feed_stale", "seq_gap", "clock_drift", "latency", "exchange_maintenance", "disk_pressure", "reconnect"}
DAILY = {"daily_loss_limit", "drawdown_limit"}
LATCHED = {"security_incident", "ledger_mismatch", "settlement_mismatch", "write_failure", "repeated_rejects",
           "rules_changed", "fee_schedule_changed", "fee_schedule_unknown", "manual_halt"}


@dataclass
class Halt:
    code: str
    since: float
    detail: str = ""

    @property
    def kind(self) -> str:
        return "latched" if self.code in LATCHED else ("daily" if self.code in DAILY else "auto")


@dataclass
class HaltBook:
    cooldown_s: float = 30.0
    active: dict[str, Halt] = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)

    def trigger(self, code: str, now: float, detail: str = "") -> None:
        if code not in self.active:
            self.active[code] = Halt(code, now, detail)
            self.log.append({"ts": now, "event": "halt", "code": code, "detail": detail})

    def clear_auto(self, code: str, now: float, reconciled: bool) -> bool:
        h = self.active.get(code)
        if h is None or h.kind != "auto":
            return False
        if not reconciled or now - h.since < self.cooldown_s:
            return False
        del self.active[code]
        self.log.append({"ts": now, "event": "recover", "code": code})
        return True

    def reset_daily(self, now: float) -> None:
        for code in [c for c, h in self.active.items() if h.kind == "daily"]:
            day_start = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            if self.active[code].since < day_start:
                del self.active[code]
                self.log.append({"ts": now, "event": "daily_reset", "code": code})

    def acknowledge(self, code: str, now: float, operator: str, cause_cleared: bool, reconciled: bool) -> bool:
        """Administrative acknowledgement for latched halts (p.16: resume only after cause cleared and
        reconciliation succeeds)."""
        h = self.active.get(code)
        if h is None or not cause_cleared or not reconciled:
            return False
        del self.active[code]
        self.log.append({"ts": now, "event": "acknowledged", "code": code, "operator": operator})
        return True

    @property
    def entries_blocked(self) -> bool:
        return bool(self.active)

    def codes(self) -> list[str]:
        return sorted(self.active)
