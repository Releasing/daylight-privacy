"""Deterministic evidence (confidence) score 0–100 (spec p.52–53, p.87, p.107).

C = Σ wᵢ·sᵢ / 100 with weights summing to 100 and each sᵢ ∈ [0, 100]. Confidence measures evidence
quality — it is not a probability, not permission to trade and never overrides EV or risk gates.
Missing *required* inputs reject (no neutral fill-in). Weights are frozen per approved version.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

WEIGHTS_V1 = {
    "strike_context": 25,  # replaced by target_before_stop for scalps
    "trend_momentum": 20,
    "order_flow": 15,
    "expected_profitability": 15,
    "multi_timeframe": 10,
    "execution_quality": 10,
    "volatility": 5,
}
REQUIRED = ("strike_context", "expected_profitability", "execution_quality")


def weights_version(w: dict[str, int]) -> str:
    return hashlib.sha256(json.dumps(w, sort_keys=True).encode()).hexdigest()[:12]


def combine(scores: dict[str, float | None], weights: dict[str, int] = WEIGHTS_V1) -> float | None:
    if sum(weights.values()) != 100:
        raise ValueError("weights must total 100")
    for k in REQUIRED:
        if scores.get(k) is None:
            return None
    total = 0.0
    used = 0
    for k, w in weights.items():
        s = scores.get(k)
        if s is None:
            continue  # optional evidence missing: contributes nothing (not a neutral 50)
        if not (0.0 <= s <= 100.0) or not math.isfinite(s):
            return None
        total += w * s
        used += w
    return total / 100.0


def _sig(x: float, k: float = 1.0) -> float:
    return 50.0 + 50.0 * math.tanh(k * x)


@dataclass
class ConfidenceInputs:
    side: str  # "yes" | "no"
    z_to_strike: float | None  # (proxy − strike)/(σ_$·√T); positive favours YES
    trend: float | None  # robust slope in σ units per minute, positive = up
    flow_imbalance: float | None  # [-1,1], positive = buying
    ev_per_dollar: float | None
    mtf_agreement: float | None  # fraction in [0,1] of timeframes whose EMA9>EMA21 (for YES)
    spread: float | None  # dollars
    quote_age_ms: float | None
    depth_ok: bool | None
    vol_regime: float | None  # realized/typical volatility ratio
    jump: bool = False
    liquidity_penalty: float = 0.0  # [0,1] from integrity layer
    target_before_stop: float | None = None  # scalp replacement for strike context


@dataclass
class ConfidenceResult:
    score: float | None
    components: dict[str, float | None] = field(default_factory=dict)
    version: str = weights_version(WEIGHTS_V1)

    def to_wire(self) -> dict:
        return {"score": None if self.score is None else round(self.score, 1),
                "components": {k: (None if v is None else round(v, 1)) for k, v in self.components.items()},
                "version": self.version}


def score(ci: ConfidenceInputs, scalp: bool = False) -> ConfidenceResult:
    sgn = 1.0 if ci.side == "yes" else -1.0
    comp: dict[str, float | None] = {}
    if scalp:
        comp["strike_context"] = None if ci.target_before_stop is None else 100.0 * ci.target_before_stop
    else:
        comp["strike_context"] = None if ci.z_to_strike is None else _sig(sgn * ci.z_to_strike, 0.8)
    comp["trend_momentum"] = None if ci.trend is None else _sig(sgn * ci.trend, 1.0)
    comp["order_flow"] = None if ci.flow_imbalance is None else 50.0 + 50.0 * sgn * ci.flow_imbalance
    comp["expected_profitability"] = None if ci.ev_per_dollar is None else _sig(ci.ev_per_dollar, 8.0)
    if ci.mtf_agreement is None:
        comp["multi_timeframe"] = None
    else:
        agree = ci.mtf_agreement if ci.side == "yes" else 1.0 - ci.mtf_agreement
        comp["multi_timeframe"] = 100.0 * agree
    if ci.spread is None or ci.quote_age_ms is None or ci.depth_ok is None:
        comp["execution_quality"] = None
    else:
        eq = 100.0 - min(60.0, ci.spread * 100 * 8) - min(30.0, ci.quote_age_ms / 100.0) - (0 if ci.depth_ok else 40)
        comp["execution_quality"] = max(0.0, eq) * (1.0 - 0.5 * ci.liquidity_penalty)
    if ci.vol_regime is None:
        comp["volatility"] = None
    else:
        comp["volatility"] = 10.0 if ci.jump else max(0.0, 100.0 - 60.0 * abs(math.log(max(ci.vol_regime, 1e-6))))
    for k, v in comp.items():
        if v is not None:
            comp[k] = max(0.0, min(100.0, v))
    return ConfidenceResult(combine(comp), comp)
