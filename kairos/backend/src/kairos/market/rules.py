"""Contract rules, settlement arithmetic and the window clock (spec p.2, p.26–27, p.82, p.106).

The BTC 15-minute contract settles on a 60-second simple average of the CF Benchmarks index before
close compared with the target (price to beat). The exact inequality / tie policy is parsed from the
market's rules text and must match an operator-verified template before the market is tradable.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from ..kalshi.parse import MarketMeta


class RulesError(RuntimeError):
    pass


_NUM = re.compile(r"[\d][\d,]*(\.\d+)?")
_TIME = re.compile(r"\b\d{1,2}:\d{2}\s*(AM|PM|am|pm)?\b")
_DATE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(,\s*\d{4})?", re.I)


def rules_template(text: str) -> str:
    """Normalize rules text by removing market-specific numbers/dates so every window of the series maps
    to one template hash."""
    t = _DATE.sub("<DATE>", text)
    t = _TIME.sub("<TIME>", t)
    t = _NUM.sub("<N>", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return hashlib.sha256(t.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SettlementRule:
    comparator: str  # ">=" or ">"
    averaging_seconds: int
    source: str  # e.g. "CF Benchmarks BRTI"
    template: str

    def yes_wins(self, settlement_average: Decimal, strike: Decimal) -> bool:
        return settlement_average >= strike if self.comparator == ">=" else settlement_average > strike


def parse_rule(meta: MarketMeta) -> SettlementRule:
    text = f"{meta.rules_primary} {meta.rules_secondary}".lower()
    if not text.strip():
        raise RulesError("rules missing")
    if "at least" in text or "greater than or equal" in text or "or above" in text or "≥" in text:
        comp = ">="
    elif "above" in text or "greater than" in text or "higher than" in text:
        comp = ">"
    else:
        raise RulesError("cannot determine settlement inequality")
    words = {"thirty": 30, "sixty": 60, "ninety": 90, "one hundred twenty": 120}
    m = re.search(r"(\d+|thirty|sixty|ninety|one hundred twenty)[\s-]*(seconds|second)", text)
    if m:
        g = m.group(1)
        avg = int(g) if g.isdigit() else words[g]
    else:
        avg = 60 if ("one minute" in text or "1 minute" in text or "one-minute" in text) and "average" in text else 0
    if avg <= 0:
        raise RulesError("cannot determine averaging window")
    src = "CF Benchmarks" if "cf benchmarks" in text else ("BRTI" if "brti" in text else "unverified")
    return SettlementRule(comp, avg, src, rules_template(meta.rules_primary + " " + meta.rules_secondary))


def required_remaining_average(strike: Decimal, observed_sum: Decimal, n_observed: int, n_total: int = 60) -> Decimal | None:
    """Average the remaining samples must reach for the final average to equal ``strike`` (p.2):
    (n_total·K − Σobserved)/(n_total − n). Returns None when every sample is observed."""
    if n_observed < 0 or n_observed > n_total:
        raise RulesError("invalid sample count")
    if n_observed == n_total:
        return None
    return (Decimal(n_total) * strike - observed_sum) / Decimal(n_total - n_observed)


@dataclass
class FinalMinuteSampler:
    """Collects one proxy observation per second over the averaging window. Missing seconds are kept
    as missing (never zero, p.27)."""

    window_start: float  # epoch seconds
    n_total: int = 60
    samples: dict[int, Decimal] = field(default_factory=dict)

    def observe(self, ts: float, value: Decimal) -> None:
        idx = int(ts - self.window_start)
        if 0 <= idx < self.n_total and idx not in self.samples:
            self.samples[idx] = value

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def total(self) -> Decimal:
        return sum(self.samples.values(), Decimal(0))

    def missing(self, now: float) -> int:
        elapsed = min(self.n_total, max(0, int(now - self.window_start)))
        return elapsed - sum(1 for i in self.samples if i < elapsed)


@dataclass(frozen=True)
class WindowClock:
    """Server-clock view of one window. All times UTC epoch seconds."""

    open_ts: float
    close_ts: float
    expiration_ts: float | None

    @classmethod
    def from_meta(cls, meta: MarketMeta) -> "WindowClock":
        if meta.open_time is None or meta.close_time is None:
            raise RulesError("market clock incomplete")
        exp = meta.expiration_time.timestamp() if meta.expiration_time else None
        return cls(meta.open_time.timestamp(), meta.close_time.timestamp(), exp)

    def seconds_to_close(self, now: float) -> float:
        return self.close_ts - now

    def elapsed(self, now: float) -> float:
        return now - self.open_ts

    def phase(self, now: float) -> str:
        if now < self.open_ts:
            return "pre_open"
        if now >= self.close_ts:
            return "closed"
        if self.close_ts - now <= 60:
            return "final_minute"
        return "open"

    def bucket(self, now: float) -> str:
        """Time-to-close buckets of p.10 measured in minutes elapsed."""
        m = self.elapsed(now) / 60.0
        if self.close_ts - now <= 60:
            return "final_minute"
        if m < 3:
            return "0-3"
        if m < 7:
            return "3-7"
        if m < 11:
            return "7-11"
        return "11-14"


def utcnow() -> float:
    return datetime.now(timezone.utc).timestamp()
