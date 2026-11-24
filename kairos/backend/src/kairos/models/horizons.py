"""Multi-horizon BTC direction forecasts (spec p.83).

Baseline: EWMA volatility with drift shrunk toward zero; P(return_h > 0) = Φ(μh / (σ√h)). Ties at the
1-second horizon are possible and handled by the caller's label definition ("no change" excluded).
Learned challengers (logistic regression / LightGBM) run in shadow via ``research.learning`` and never
replace this output without promotion.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm

HORIZONS = (1, 5, 15, 30, 60, 180)


@dataclass(frozen=True)
class HorizonForecast:
    horizon_s: int
    p_up: float
    sigma: float
    model: str = "ewma_gaussian_v1"


def forecast(drift_per_s: float | None, sigma_per_s: float | None, seconds_left: float, shrink: float = 0.5) -> list[HorizonForecast]:
    if sigma_per_s is None or sigma_per_s <= 0:
        return []
    mu = (drift_per_s or 0.0) * (1 - shrink)
    out = []
    for h in HORIZONS:
        if h > seconds_left:
            continue
        sd = sigma_per_s * math.sqrt(h)
        out.append(HorizonForecast(h, float(norm.cdf(mu * h / sd)), sd))
    return out
