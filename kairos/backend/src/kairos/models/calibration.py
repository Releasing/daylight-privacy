"""Probability scoring and calibration (spec p.9, p.58, p.83)."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def brier(p: np.ndarray, y: np.ndarray) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray, eps: float = 1e-12) -> float:
    p = np.clip(np.asarray(p, float), eps, 1 - eps)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def reliability(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict]:
    p, y = np.asarray(p, float), np.asarray(y, float)
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for i in range(bins):
        m = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        n = int(m.sum())
        out.append({"lo": float(edges[i]), "hi": float(edges[i + 1]), "n": n,
                    "pred": float(p[m].mean()) if n else None, "obs": float(y[m].mean()) if n else None})
    return out


def ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    rel = reliability(p, y, bins)
    n = sum(b["n"] for b in rel)
    if n == 0:
        return float("nan")
    return float(sum(b["n"] / n * abs(b["pred"] - b["obs"]) for b in rel if b["n"]))


@dataclass
class PlattCalibrator:
    """Logistic recalibration p' = σ(a·logit(p)+b) fitted on a chronologically later validation block."""

    a: float = 1.0
    b: float = 0.0
    fitted: bool = False

    def fit(self, p: np.ndarray, y: np.ndarray, iters: int = 200, lr: float = 0.1) -> "PlattCalibrator":
        x = np.log(np.clip(p, 1e-6, 1 - 1e-6) / np.clip(1 - p, 1e-6, 1))
        y = np.asarray(y, float)
        a, b = 1.0, 0.0
        for _ in range(iters):
            z = 1 / (1 + np.exp(-(a * x + b)))
            g = z - y
            a -= lr * float(np.mean(g * x))
            b -= lr * float(np.mean(g))
        self.a, self.b, self.fitted = a, b, True
        return self

    def __call__(self, p: float) -> float:
        if not self.fitted:
            return p
        x = math.log(min(max(p, 1e-6), 1 - 1e-6) / (1 - min(max(p, 1e-6), 1 - 1e-6)))
        return 1 / (1 + math.exp(-(self.a * x + self.b)))


def shrink(p: float, baseline: float, weight: float) -> float:
    """Shrink an uncertain prediction toward a baseline (p.83). weight ∈ [0,1] is trust in ``p``."""
    w = min(1.0, max(0.0, weight))
    return w * p + (1 - w) * baseline
