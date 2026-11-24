"""Edge registry and strategy lifecycle (spec p.102, p.109).

shadow → eligible → active → reduced → suspended, with hysteresis. Transitions require pre-declared
evidence; deterioration downgrades immediately; upgrades need sustained evidence. Only bounded,
pre-authorized BTC configurations may change automatically.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

STATES = ("suspended", "reduced", "shadow", "eligible", "active")


@dataclass
class EdgeRecord:
    strategy: str
    version: str
    status: str = "shadow"
    horizon: str = ""
    regime: str = "all"
    samples: int = 0
    windows: int = 0
    net_ev: float = 0.0
    ev_lcb: float = 0.0  # lower confidence bound (day-block bootstrap)
    uncertainty: float = 0.0
    capacity_contracts: int = 0
    last_validation: float | None = None
    good_streak: int = 0
    history: list[tuple[float, str, str]] = field(default_factory=list)

    def to_wire(self) -> dict:
        d = asdict(self)
        d["history"] = self.history[-10:]
        return d


@dataclass
class Gates:
    min_samples: int = 300
    min_windows: int = 1000
    min_days: int = 30
    upgrade_streak: int = 3


class EdgeRegistry:
    def __init__(self, gates: Gates | None = None):
        self.gates = gates or Gates()
        self.records: dict[str, EdgeRecord] = {}

    def register(self, strategy: str, version: str, horizon: str = "") -> EdgeRecord:
        rec = self.records.get(strategy)
        if rec is None or rec.version != version:
            rec = EdgeRecord(strategy, version, horizon=horizon)
            self.records[strategy] = rec
        return rec

    def _move(self, rec: EdgeRecord, new: str, ts: float, why: str) -> None:
        if new != rec.status:
            rec.history.append((ts, f"{rec.status}->{new}", why))
            rec.status = new

    def update(self, strategy: str, ts: float, samples: int, windows: int, days: float, net_ev: float, ev_lcb: float,
               drift: bool = False, risk_violation: bool = False) -> EdgeRecord:
        rec = self.records[strategy]
        rec.samples, rec.windows, rec.net_ev, rec.ev_lcb, rec.last_validation = samples, windows, net_ev, ev_lcb, ts
        g = self.gates
        if risk_violation:
            self._move(rec, "suspended", ts, "risk_violation")
            rec.good_streak = 0
            return rec
        if drift or ev_lcb < 0:
            # immediate downgrade on adverse evidence
            target = "reduced" if rec.status == "active" else ("suspended" if rec.status == "reduced" else rec.status)
            if rec.status in ("eligible",):
                target = "shadow"
            self._move(rec, target, ts, "drift" if drift else "negative_lcb")
            rec.good_streak = 0
            return rec
        enough = samples >= g.min_samples and windows >= g.min_windows and days >= g.min_days
        if not enough:
            return rec  # insufficient evidence → stay (no promotion after a winning streak)
        rec.good_streak += 1
        if rec.good_streak < g.upgrade_streak:
            return rec
        nxt = {"suspended": "shadow", "shadow": "eligible", "reduced": "active", "eligible": "eligible"}.get(rec.status, rec.status)
        # eligible → active requires administrative release for live; paper may treat eligible as active
        self._move(rec, nxt, ts, "sustained_positive_lcb")
        rec.good_streak = 0
        return rec

    def live_allowed(self, strategy: str) -> bool:
        r = self.records.get(strategy)
        return bool(r and r.status in ("active", "reduced"))
