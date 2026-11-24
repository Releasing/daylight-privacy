"""Fast closed-form contract fair value for the averaged settlement (spec p.84, p.90).

Used inside path simulations where nested Monte Carlo is too slow. It approximates the distribution of
the 60-second average as Gaussian:

* more than 60 s left:  mean = S,  var = σ$²·(R − 60) + σ$²·60/3
* inside the window:    mean = (Σobs + S·R)/60,  var = σ$²·R³/(3·60²)

plus basis variance. Its error versus the Monte Carlo model is bounded in tests
(``test_models.py::test_fair_value_matches_mc``).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.special import ndtr


def fair_yes(S: np.ndarray | float, K: float, R: np.ndarray | float, sigma_dollar: np.ndarray | float,
             obs_sum: float = 0.0, n_obs: int = 0, basis_sd: float = 0.0, avg_s: int = 60, comparator: str = ">=") -> np.ndarray:
    S = np.asarray(S, float)
    R = np.maximum(np.asarray(R, float), 0.0)
    sd2 = np.asarray(sigma_dollar, float) ** 2
    outside = R > avg_s
    mean_out = S
    var_out = sd2 * (R - avg_s) + sd2 * avg_s / 3.0
    # inside window: remaining R seconds unobserved, avg_s − R observed (or missing → approximated by S)
    known = obs_sum if n_obs > 0 else 0.0
    filled_missing = S * np.maximum(avg_s - n_obs - R, 0.0)
    mean_in = (known + filled_missing + S * R) / avg_s
    var_in = sd2 * R ** 3 / (3.0 * avg_s ** 2)
    mean = np.where(outside, mean_out, mean_in)
    var = np.where(outside, var_out, var_in) + basis_sd ** 2
    sd = np.sqrt(np.maximum(var, 1e-18))
    z = (mean - K) / sd
    p = ndtr(z)
    return np.clip(p, 0.0, 1.0)


def fair_yes_scalar(*args, **kwargs) -> float:
    return float(fair_yes(*args, **kwargs))


def sigma_dollar(spot: float, sigma_log_per_s: float) -> float:
    return spot * sigma_log_per_s if math.isfinite(sigma_log_per_s) else float("nan")
