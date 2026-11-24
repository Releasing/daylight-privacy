"""Causal support/resistance discovery and scoring (spec p.104, p.123–125).

* Swings are confirmed only after ``right`` completed bars; ``detected_at`` (confirmation time) is
  stored separately from ``pivot_at``.
* Nearby candidates cluster within ``max(tick, 0.15·ATR)``.
* Strength 0–100 combines normalized reaction count, recency, volume, timeframe significance and
  independent confirmation. It is **not a probability**.
* Lifecycle: active → broken → flipped (role reversal after a confirmed break + retest) → stale.
Every change is versioned for causal replay.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from ..features.candles import Bar

TF_WEIGHT = {5: 0.25, 60: 0.45, 300: 0.65, 900: 0.8, 3600: 0.9, 14400: 1.0}
_ids = itertools.count(1)


@dataclass
class Level:
    id: int
    asset: str
    timeframe: int
    source: str  # swing_high | swing_low | prior_extreme | vwap | pivot | book_depth
    kind: str  # support | resistance
    center: float
    lower: float
    upper: float
    pivot_at: int
    detected_at: int
    last_touch_at: int
    touches: int = 1
    volume: float = 0.0
    confirmations: set[str] = field(default_factory=set)
    status: str = "active"  # active | broken | flipped | stale
    broken_at: int | None = None
    strength: float = 0.0
    version: int = 1

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def to_wire(self) -> dict:
        return {
            "id": self.id, "asset": self.asset, "timeframe": self.timeframe, "source": self.source, "kind": self.kind,
            "center": round(self.center, 2), "lower": round(self.lower, 2), "upper": round(self.upper, 2),
            "detected_at": self.detected_at, "last_touch_at": self.last_touch_at, "touches": self.touches,
            "strength": round(self.strength, 1), "status": self.status, "version": self.version,
        }


@dataclass
class LevelEngine:
    asset: str = "BTC"
    left: int = 3
    right: int = 3
    tick: float = 0.01
    tol_atr: float = 0.15
    stale_after_s: int = 6 * 3600
    max_levels: int = 40
    levels: list[Level] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)  # change log for replay

    def _tol(self, atr: float | None) -> float:
        return max(self.tick, self.tol_atr * atr) if atr else self.tick * 10

    def on_bar(self, bars: list[Bar], atr: float | None, now: int) -> list[Level]:
        """Call after each completed bar of one timeframe. Uses only completed bars."""
        changed: list[Level] = []
        if len(bars) < self.left + self.right + 1:
            return changed
        tf = bars[-1].tf
        tol = self._tol(atr)
        # a pivot at index -(right+1) is confirmed by the latest bar
        i = len(bars) - self.right - 1
        piv = bars[i]
        window = bars[i - self.left: i + self.right + 1]
        confirm_time = bars[-1].start + tf
        if piv.high == max(b.high for b in window) and sum(1 for b in window if b.high == piv.high) == 1:
            changed.append(self._add(tf, "swing_high", "resistance", piv.high, tol, piv.start, confirm_time, piv.volume))
        if piv.low == min(b.low for b in window) and sum(1 for b in window if b.low == piv.low) == 1:
            changed.append(self._add(tf, "swing_low", "support", piv.low, tol, piv.start, confirm_time, piv.volume))
        changed.extend(self._lifecycle(bars[-1], tol, now))
        self._score(now)
        return changed

    def add_reference(self, source: str, price: float, atr: float | None, now: int, tf: int = 60) -> Level:
        """Independent confirmations: VWAP, pivots, prior completed extremes, persistent book depth."""
        kind = "support"
        lv = self._add(tf, source, kind, price, self._tol(atr), now, now, 0.0)
        self._score(now)
        return lv

    def _add(self, tf: int, source: str, kind: str, price: float, tol: float, pivot_at: int, detected_at: int, vol: float) -> Level:
        for lv in self.levels:
            if lv.status != "stale" and abs(lv.center - price) <= tol:
                # merge: weighted center, widen zone, count reaction
                w = lv.touches
                lv.center = (lv.center * w + price) / (w + 1)
                lv.lower = min(lv.lower, price - tol / 2)
                lv.upper = max(lv.upper, price + tol / 2)
                lv.touches += 1
                lv.volume += vol
                lv.last_touch_at = max(lv.last_touch_at, detected_at)
                lv.timeframe = max(lv.timeframe, tf)
                lv.confirmations.add(source)
                lv.version += 1
                self._log(lv, "merge")
                return lv
        lv = Level(next(_ids), self.asset, tf, source, kind, price, price - tol / 2, price + tol / 2, pivot_at, detected_at,
                   detected_at, 1, vol, {source})
        self.levels.append(lv)
        self._log(lv, "new")
        if len(self.levels) > self.max_levels:
            self.levels.sort(key=lambda l: (l.status == "stale", -l.strength))
            for dropped in self.levels[self.max_levels:]:
                dropped.status = "stale"
                self._log(dropped, "evict")
            self.levels = self.levels[: self.max_levels]
        return lv

    def _lifecycle(self, bar: Bar, tol: float, now: int) -> list[Level]:
        changed = []
        for lv in self.levels:
            if lv.status == "stale":
                continue
            if now - lv.last_touch_at > self.stale_after_s:
                lv.status = "stale"
                lv.version += 1
                self._log(lv, "stale")
                changed.append(lv)
                continue
            # classify role relative to price
            if lv.status in ("active", "flipped"):
                if lv.kind == "resistance" and bar.close > lv.upper:
                    lv.status, lv.broken_at = "broken", bar.start
                elif lv.kind == "support" and bar.close < lv.lower:
                    lv.status, lv.broken_at = "broken", bar.start
                elif lv.contains(bar.high) or lv.contains(bar.low):
                    lv.touches += 1
                    lv.last_touch_at = bar.start
                else:
                    continue
                lv.version += 1
                self._log(lv, lv.status)
                changed.append(lv)
            elif lv.status == "broken":
                # retest from the other side holding → role reversal
                if lv.kind == "resistance" and lv.contains(bar.low) and bar.close > lv.upper:
                    lv.kind, lv.status = "support", "flipped"
                elif lv.kind == "support" and lv.contains(bar.high) and bar.close < lv.lower:
                    lv.kind, lv.status = "resistance", "flipped"
                else:
                    continue
                lv.last_touch_at = bar.start
                lv.version += 1
                self._log(lv, "flip")
                changed.append(lv)
        return changed

    def _score(self, now: int) -> None:
        vols = [l.volume for l in self.levels if l.volume > 0]
        vmax = max(vols) if vols else 0.0
        for lv in self.levels:
            reactions = 1 - math.exp(-(lv.touches - 1) / 3.0)
            recency = math.exp(-max(0, now - lv.last_touch_at) / 7200.0)
            volume = (lv.volume / vmax) if vmax > 0 else 0.0
            tfw = TF_WEIGHT.get(lv.timeframe, 0.5)
            conf = min(1.0, (len(lv.confirmations) - 1) / 2.0)
            s = 100 * (0.30 * reactions + 0.20 * recency + 0.15 * volume + 0.20 * tfw + 0.15 * conf)
            if lv.status == "stale":
                s *= 0.2
            elif lv.status == "broken":
                s *= 0.5
            lv.strength = max(0.0, min(100.0, s))

    def _log(self, lv: Level, event: str) -> None:
        self.history.append({"event": event, **lv.to_wire()})

    def nearest(self, price: float, n: int = 2) -> dict[str, list[Level]]:
        live = [l for l in self.levels if l.status in ("active", "flipped")]
        sup = sorted([l for l in live if l.kind == "support" and l.center <= price], key=lambda l: price - l.center)[:n]
        res = sorted([l for l in live if l.kind == "resistance" and l.center >= price], key=lambda l: l.center - price)[:n]
        return {"support": sup, "resistance": res}

    def features(self, price: float, atr: float | None) -> dict[str, float | None]:
        nb = self.nearest(price, 1)
        out: dict[str, float | None] = {}
        for k in ("support", "resistance"):
            if nb[k] and atr:
                lv = nb[k][0]
                out[f"dist_{k}_atr"] = abs(price - lv.center) / atr
                out[f"{k}_strength"] = lv.strength
                out[f"{k}_touches"] = float(lv.touches)
            else:
                out[f"dist_{k}_atr"] = out[f"{k}_strength"] = out[f"{k}_touches"] = None
        return out
