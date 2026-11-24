"""ETH/SOL read-only cross-asset intelligence for BTC (spec p.101, p.105).

Synchronized normalized returns, rolling correlation, residual divergence and lag features computed on
past data only. When an auxiliary feed is stale, features are ``None`` and the predictor uses the
validated BTC-only model (fallback, not a block).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class CrossAsset:
    window: int = 120
    series: dict[str, deque] = field(default_factory=lambda: {a: deque(maxlen=601) for a in ("BTC", "ETH", "SOL")})
    last_sec: dict[str, int] = field(default_factory=dict)

    def update(self, asset: str, ts: float, price: float) -> None:
        sec = int(ts)
        if asset not in self.series or price <= 0:
            return
        last = self.last_sec.get(asset)
        if last is not None and sec <= last:
            return
        self.series[asset].append((sec, price))
        self.last_sec[asset] = sec

    def _aligned_returns(self, a: str, b: str, n: int, lag: int = 0) -> tuple[list[float], list[float]] | None:
        sa = dict(self.series[a])
        sb = dict(self.series[b])
        secs = sorted(set(sa) & set(sb))
        if len(secs) < n + lag + 2:
            return None
        secs = secs[-(n + lag + 1):]
        ra, rb = [], []
        for i in range(1 + lag, len(secs)):
            t1, t0 = secs[i], secs[i - 1]
            u1, u0 = secs[i - lag], secs[i - lag - 1]
            ra.append(math.log(sa[t1] / sa[t0]))
            rb.append(math.log(sb[u1] / sb[u0]))
        return ra, rb

    @staticmethod
    def _corr(x: list[float], y: list[float]) -> float | None:
        n = len(x)
        if n < 10:
            return None
        mx, my = sum(x) / n, sum(y) / n
        vx = sum((v - mx) ** 2 for v in x)
        vy = sum((v - my) ** 2 for v in y)
        if vx <= 0 or vy <= 0:
            return None
        return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / math.sqrt(vx * vy)

    def features(self, now: float, max_age_s: float = 5.0) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for aux in ("ETH", "SOL"):
            fresh = aux in self.last_sec and now - self.last_sec[aux] <= max_age_s
            if not fresh:
                out[f"{aux.lower()}_corr"] = out[f"{aux.lower()}_resid_div"] = out[f"{aux.lower()}_lead5"] = None
                continue
            ar = self._aligned_returns("BTC", aux, self.window)
            if ar is None:
                out[f"{aux.lower()}_corr"] = out[f"{aux.lower()}_resid_div"] = out[f"{aux.lower()}_lead5"] = None
                continue
            btc, alt = ar
            c = self._corr(btc, alt)
            out[f"{aux.lower()}_corr"] = c
            # residual divergence over last 30 s: alt cumulative return minus beta * btc cumulative return
            var_b = sum(v * v for v in btc)
            beta = (sum(btc[i] * alt[i] for i in range(len(btc))) / var_b) if var_b > 0 else None
            if beta:
                out[f"{aux.lower()}_resid_div"] = sum(alt[-30:]) / beta - sum(btc[-30:])
            else:
                out[f"{aux.lower()}_resid_div"] = None
            lagged = self._aligned_returns("BTC", aux, self.window, lag=5)
            out[f"{aux.lower()}_lead5"] = self._corr(*lagged) if lagged else None
        out["btc_only_fallback"] = 1.0 if all(out.get(f"{a}_corr") is None for a in ("eth", "sol")) else 0.0
        return out
