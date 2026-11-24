"""Opt-in verified performance (spec p.136, p.138).

Only reconciled ledger data, only opted-in users, pseudonymous aliases, paper and live separated.
A *trade* is a closed position episode (all fills from open to flat, partial exits and fees aggregated).
Day boundary is UTC. Percentage return uses a cash-flow-adjusted (modified Dietz) denominator:
    r = net / (start_equity + Σ w_i·flow_i),  w_i = fraction of the period remaining after the flow.
No balances, keys, identity, exact orders or live positions are published. Losses are included.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..money import ZERO

PERIODS = {"today": None, "7d": 7, "30d": 30, "all": None}


def period_start(period: str, now: float) -> float:
    if period == "today":
        return datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if period == "all":
        return 0.0
    days = PERIODS.get(period)
    if days is None:
        raise ValueError("period")
    return now - days * 86400


@dataclass
class Row:
    alias: str
    net: Decimal
    pct: float | None
    wins: int
    losses: int
    breakevens: int
    trades: int
    since: float

    @property
    def wl(self) -> float | None:
        return (self.wins / self.losses) if self.losses else None

    def to_wire(self) -> dict:
        return {"alias": self.alias, "net": str(self.net.quantize(Decimal("0.01"))), "pct": None if self.pct is None else round(self.pct * 100, 2),
                "wins": self.wins, "losses": self.losses, "breakevens": self.breakevens, "wl": None if self.wl is None else round(self.wl, 2),
                "trades": self.trades}


def episodes_from_ledger(events: list[dict]) -> list[tuple[float, Decimal]]:
    """Aggregate realized P&L by (market, side, episode) and return (close_ts, net) per closed episode."""
    agg: dict[tuple, list] = defaultdict(lambda: [0.0, ZERO, False])
    for e in events:
        if e["event_type"] not in ("fill", "settle") or e.get("realized_pnl") is None or e.get("episode") is None:
            continue
        k = (e["market_id"], e.get("strategy"), e["episode"])
        a = agg[k]
        a[0] = max(a[0], e["ts"])
        a[1] += Decimal(str(e["realized_pnl"]))
        if e.get("bucket_delta") and e["bucket_delta"].get("episode_closed"):
            a[2] = True
    return [(ts, net) for ts, net, closed in agg.values() if closed]


def build(users: list[dict], ledger_loader, paper: bool, period: str, now: float | None = None) -> dict:
    now = now or time.time()
    start = period_start(period, now)
    rows: list[Row] = []
    for u in users:
        if not u.get("community_opt_in") or not u.get("community_alias"):
            continue  # default private
        events = ledger_loader(u["id"], paper)
        eps = [(ts, n) for ts, n in episodes_from_ledger(events) if ts >= start]
        start_equity = Decimal(str(u.get("paper_start_equity") or 0)) if paper else ZERO
        flows_w = ZERO
        span = max(now - start, 1.0) if start > 0 else None
        for e in events:
            if e["event_type"] == "deposit":
                if e["ts"] < start:
                    start_equity += Decimal(str(e.get("price") or 0))
                elif span:
                    flows_w += Decimal(str(e.get("price") or 0)) * Decimal(str((now - e["ts"]) / span))
            elif e["event_type"] in ("fill", "settle") and e["ts"] < start and e.get("realized_pnl") is not None:
                start_equity += Decimal(str(e["realized_pnl"]))
        net = sum((n for _, n in eps), ZERO)
        denom = start_equity + flows_w
        pct = float(net / denom) if denom > 0 else None
        rows.append(Row(u["community_alias"], net, pct, sum(1 for _, n in eps if n > 0), sum(1 for _, n in eps if n < 0),
                        sum(1 for _, n in eps if n == 0), len(eps), start))
    rows.sort(key=lambda r: r.net, reverse=True)
    total_trades = sum(r.trades for r in rows)
    return {"period": period, "mode": "paper" if paper else "live", "since": start, "generated_ts": now, "day_boundary": "UTC",
            "rows": [r.to_wire() for r in rows], "participants": len(rows), "sample_trades": total_trades,
            "aggregate": {"net": str(sum((r.net for r in rows), ZERO).quantize(Decimal("0.01"))),
                          "wins": sum(r.wins for r in rows), "losses": sum(r.losses for r in rows),
                          "breakevens": sum(r.breakevens for r in rows)}}
