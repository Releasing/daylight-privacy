"""Pattern events against a level zone (spec p.125, p.128).

Support bounce, resistance rejection, breakout, breakdown, retest and false breakout are separate
events computed from completed bars only. Each event carries quantified evidence (wick/body, volume
delta, velocity, time since crossing). These are model inputs — never automatic trades.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..features.candles import Bar
from .detect import Level


@dataclass(frozen=True)
class PatternEvent:
    kind: str  # support_bounce | resistance_rejection | breakout | breakdown | retest | false_breakout
    level_id: int
    at: int
    wick_body: float | None
    volume_delta: float
    velocity: float
    since_cross_s: int | None

    def to_wire(self) -> dict:
        return {"kind": self.kind, "level_id": self.level_id, "at": self.at, "wick_body": self.wick_body,
                "volume_delta": self.volume_delta, "velocity": self.velocity, "since_cross_s": self.since_cross_s}


def _wick_body(b: Bar, lower: bool) -> float | None:
    body = abs(b.close - b.open)
    wick = (min(b.open, b.close) - b.low) if lower else (b.high - max(b.open, b.close))
    if body == 0:
        return None if wick == 0 else float("inf")
    return wick / body


def detect(bars: list[Bar], lv: Level, persist: int = 2, false_horizon: int = 3) -> list[PatternEvent]:
    """Scan the most recent completed bars for events at ``lv``. ``persist`` closes beyond the zone
    are required for a breakout; return inside the zone within ``false_horizon`` bars is a false breakout."""
    out: list[PatternEvent] = []
    if len(bars) < 2:
        return out
    b = bars[-1]
    prev = bars[-2]
    vel = (b.close - prev.close) / b.tf
    vd = b.buy_volume - b.sell_volume
    # bounce / rejection on the last bar
    if lv.kind == "support" and b.low <= lv.upper and b.close > lv.upper:
        out.append(PatternEvent("support_bounce", lv.id, b.start, _wick_body(b, True), vd, vel, None))
    if lv.kind == "resistance" and b.high >= lv.lower and b.close < lv.lower:
        out.append(PatternEvent("resistance_rejection", lv.id, b.start, _wick_body(b, False), vd, vel, None))
    # crossing analysis over a short history
    hist = bars[-(persist + false_horizon + 1):]
    above = [x.close > lv.upper for x in hist]
    below = [x.close < lv.lower for x in hist]
    # breakout: was not above, then `persist` consecutive closes above
    if len(hist) > persist and all(above[-persist:]) and not above[-persist - 1]:
        kind = "breakout" if lv.kind == "resistance" else "reclaim"
        if kind == "breakout":
            out.append(PatternEvent("breakout", lv.id, b.start, None, vd, vel, persist * b.tf))
    if len(hist) > persist and all(below[-persist:]) and not below[-persist - 1]:
        if lv.kind == "support":
            out.append(PatternEvent("breakdown", lv.id, b.start, None, vd, vel, persist * b.tf))
    # false breakout: crossed beyond, then back inside the zone within horizon
    for j in range(len(hist) - 1):
        crossed_up = hist[j].close > lv.upper and (j == 0 or hist[j - 1].close <= lv.upper)
        crossed_dn = hist[j].close < lv.lower and (j == 0 or hist[j - 1].close >= lv.lower)
        if (crossed_up or crossed_dn) and lv.contains(b.close) and (len(hist) - 1 - j) <= false_horizon and j < len(hist) - 1:
            out.append(PatternEvent("false_breakout", lv.id, b.start, None, vd, vel, (len(hist) - 1 - j) * b.tf))
            break
    # retest: level previously broken/flipped and price touches it from the new side
    if lv.status == "flipped" and lv.contains(b.low if lv.kind == "support" else b.high):
        out.append(PatternEvent("retest", lv.id, b.start, _wick_body(b, lv.kind == "support"), vd, vel, None))
    return out
