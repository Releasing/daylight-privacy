"""Settlement probability for averaged-benchmark binary contracts (spec p.8, p.27–28, p.83–84).

The contract resolves on the 60-second simple average of the official index before close compared to
the strike. We simulate per-second proxy paths, combine with already-observed final-window samples,
add a per-path proxy→index basis draw and apply the verified comparator.

Return models (challengers, p.8): ``gaussian`` (baseline), ``student_t`` (heavy tail, unit variance),
``bootstrap`` (empirical block-resampled returns, preserves short-range clustering).

Uncertainty is reported separately: Monte-Carlo standard error √(p(1−p)/N) and a model band obtained by
re-running with stressed volatility and basis (common random numbers). ``p_conservative`` is the lower
envelope minus 2·SE — the value used by economics for the side being bought.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import norm


def phi_baseline(S: float, K: float, mu: float, sigma: float, T: float) -> float:
    """Point-price teaching benchmark P(up) = Φ((S−K+μT)/(σ√T)); S,K $, μ $/s, σ $/√s, T s (p.8).
    Not the averaged settlement model."""
    if T <= 0 or sigma <= 0:
        x = S - K + mu * max(T, 0.0)
        return 1.0 if x > 0 else (0.0 if x < 0 else 0.5)
    return float(norm.cdf((S - K + mu * T) / (sigma * math.sqrt(T))))


@dataclass
class BasisTracker:
    """Proxy-minus-official settlement-average basis in dollars, learned from settled windows."""

    prior_sd: float = 15.0
    min_samples: int = 30
    samples: list[float] = field(default_factory=list)

    def add(self, proxy_avg: float, official_value: float) -> None:
        self.samples.append(official_value - proxy_avg)
        if len(self.samples) > 2000:
            self.samples.pop(0)

    @property
    def mean(self) -> float:
        return float(np.mean(self.samples)) if len(self.samples) >= self.min_samples else 0.0

    @property
    def sd(self) -> float:
        if len(self.samples) >= self.min_samples:
            return max(float(np.std(self.samples, ddof=1)), 1.0)
        return self.prior_sd


@dataclass(frozen=True)
class SettlementInputs:
    spot: float  # current proxy price ($)
    strike: float  # official target ($)
    now: float  # epoch s
    close_ts: float  # epoch s
    sigma_per_s: float  # log-return sd per second
    drift_per_s: float = 0.0  # log drift per second (shrunk; default no-drift)
    avg_seconds: int = 60
    observed: dict[int, float] | None = None  # second index within averaging window → proxy value
    comparator: str = ">="
    basis_mean: float = 0.0
    basis_sd: float = 15.0


@dataclass(frozen=True)
class SettlementEstimate:
    p_yes: float
    mc_se: float
    p_low: float
    p_high: float
    p_conservative_yes: float
    p_conservative_no: float
    n_paths: int
    model: str
    determined: bool
    missing_samples: int
    valid: bool
    reason: str | None = None

    def to_wire(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


class SettlementModel:
    def __init__(self, n_paths: int = 4000, seed: int = 20261124, model: str = "gaussian",
                 empirical_returns: np.ndarray | None = None, block: int = 10, max_missing: int = 10):
        self.n = int(n_paths)
        self.seed = int(seed)
        self.model = model
        self.emp = empirical_returns
        self.block = block
        self.max_missing = max_missing

    # ----------------------------------------------------------------------------------------
    def _innovations(self, rng: np.random.Generator, steps: int) -> np.ndarray:
        if steps <= 0:
            return np.zeros((self.n, 0))
        if self.model == "student_t":
            nu = 4.0
            z = rng.standard_t(nu, size=(self.n, steps)) / math.sqrt(nu / (nu - 2.0))
            return z
        if self.model == "bootstrap" and self.emp is not None and len(self.emp) > self.block * 5:
            e = (self.emp - self.emp.mean()) / (self.emp.std() + 1e-18)
            nblocks = math.ceil(steps / self.block)
            starts = rng.integers(0, len(e) - self.block, size=(self.n, nblocks))
            idx = (starts[:, :, None] + np.arange(self.block)[None, None, :]).reshape(self.n, -1)[:, :steps]
            return e[idx]
        return rng.standard_normal((self.n, steps))

    def _simulate(self, x: SettlementInputs, sigma: float, basis_sd: float, rng_seed: int) -> tuple[float, bool, int]:
        rng = np.random.default_rng(rng_seed)
        T = max(0.0, x.close_ts - x.now)
        win_start = x.close_ts - x.avg_seconds
        observed = dict(x.observed or {})
        elapsed_in_win = int(min(x.avg_seconds, max(0, math.floor(x.now - win_start))))
        missing_past = [i for i in range(elapsed_in_win) if i not in observed]
        future_idx = list(range(elapsed_in_win, x.avg_seconds))
        pre_steps = int(max(0, math.ceil(win_start - x.now)))  # seconds before averaging window starts
        determined = not future_idx and not missing_past
        n_future = len(future_idx)
        z = self._innovations(rng, pre_steps + n_future)
        log_inc = x.drift_per_s + sigma * z  # per-second log returns
        cum = np.cumsum(log_inc, axis=1) if z.shape[1] else np.zeros((self.n, 0))
        paths = x.spot * np.exp(cum)  # proxy price at each future second
        fut_vals = paths[:, pre_steps:] if n_future else np.zeros((self.n, 0))
        obs_sum = sum(observed.values())
        # missing past seconds: filled with nearest observed proxy value plus noise; never zero
        if missing_past:
            ref = np.array([observed[min(observed, key=lambda k: abs(k - i))] if observed else x.spot for i in missing_past])
            fill = ref[None, :] * np.exp(sigma * math.sqrt(2.0) * rng.standard_normal((self.n, len(missing_past))))
            miss_sum = fill.sum(axis=1)
        else:
            miss_sum = 0.0
        total = obs_sum + miss_sum + (fut_vals.sum(axis=1) if n_future else 0.0)
        avg = total / x.avg_seconds
        basis = x.basis_mean + basis_sd * rng.standard_normal(self.n)
        idx_avg = avg + basis
        yes = idx_avg >= x.strike if x.comparator == ">=" else idx_avg > x.strike
        return float(np.mean(yes)), determined and basis_sd == 0, len(missing_past)

    def estimate(self, x: SettlementInputs) -> SettlementEstimate:
        if not (x.spot > 0 and x.strike > 0) or not math.isfinite(x.sigma_per_s) or x.sigma_per_s < 0:
            return SettlementEstimate(0.5, 0, 0, 1, 0, 0, 0, self.model, False, 0, False, "invalid_inputs")
        if x.comparator not in (">=", ">"):
            return SettlementEstimate(0.5, 0, 0, 1, 0, 0, 0, self.model, False, 0, False, "unknown_comparator")
        if x.now >= x.close_ts + 5:
            return SettlementEstimate(0.5, 0, 0, 1, 0, 0, 0, self.model, False, 0, False, "window_closed")
        p, det, miss = self._simulate(x, x.sigma_per_s, x.basis_sd, self.seed)
        if miss > self.max_missing:
            return SettlementEstimate(p, 0, 0, 1, 0, 0, self.n, self.model, False, miss, False, "benchmark_coverage")
        # model band: stressed volatility and basis using common random numbers (same seed)
        p_hi_vol, _, _ = self._simulate(x, x.sigma_per_s * 1.25, x.basis_sd * 1.5, self.seed)
        p_lo_vol, _, _ = self._simulate(x, x.sigma_per_s * 0.8, x.basis_sd, self.seed)
        lo, hi = min(p, p_hi_vol, p_lo_vol), max(p, p_hi_vol, p_lo_vol)
        se = math.sqrt(max(p * (1 - p), 1e-12) / self.n)
        return SettlementEstimate(
            p_yes=p, mc_se=se, p_low=lo, p_high=hi,
            p_conservative_yes=max(0.0, lo - 2 * se), p_conservative_no=max(0.0, 1 - hi - 2 * se),
            n_paths=self.n, model=self.model, determined=det, missing_samples=miss, valid=True,
        )


def buffer_dollars(z: float, sigma_dollars_per_min: float, remaining_minutes: float) -> float:
    """Baseline uncertainty buffer = z·σ·√(remaining minutes) (p.28). Stale inputs → caller invalidates."""
    if remaining_minutes <= 0:
        return 0.0
    return z * sigma_dollars_per_min * math.sqrt(remaining_minutes)
