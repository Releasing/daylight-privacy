"""Chronological splits, leakage checks and block-bootstrap statistics (spec p.9, p.17, p.43–44, p.58, p.92)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class LeakageError(ValueError):
    pass


@dataclass(frozen=True)
class Split:
    train: np.ndarray
    calib: np.ndarray
    test: np.ndarray


def chronological_split(times: np.ndarray, groups: np.ndarray, label_end: np.ndarray, frac=(0.6, 0.2, 0.2),
                        embargo_s: float = 900.0) -> Split:
    """Group-aware chronological split (contracts never straddle partitions) with purge + embargo of
    observations whose label horizon overlaps the next partition's start."""
    order = np.argsort(times, kind="stable")
    uniq = []
    seen = set()
    for g in groups[order]:
        if g not in seen:
            seen.add(g)
            uniq.append(g)
    n = len(uniq)
    a, b = int(n * frac[0]), int(n * (frac[0] + frac[1]))
    gset = [set(uniq[:a]), set(uniq[a:b]), set(uniq[b:])]
    idx = [np.where(np.isin(groups, list(s)))[0] for s in gset]
    out = []
    for k in range(3):
        ids = idx[k]
        if k < 2 and len(idx[k + 1]):
            nxt_start = times[idx[k + 1]].min()
            ids = ids[label_end[ids] + embargo_s <= nxt_start]  # purge + embargo
        out.append(ids)
    return Split(*out)


def assert_no_leakage(feature_time: np.ndarray, decision_time: np.ndarray, groups_train: np.ndarray, groups_test: np.ndarray) -> None:
    if np.any(feature_time > decision_time):
        raise LeakageError("feature computed after decision time")
    if set(groups_train.tolist()) & set(groups_test.tolist()):
        raise LeakageError("same market in train and test")


def block_bootstrap_mean(values: np.ndarray, blocks: np.ndarray, n_boot: int = 2000, alpha: float = 0.05, seed: int = 1) -> tuple[float, float, float]:
    """Mean and (1−α) CI resampling whole blocks (days / market sessions) — trades within a window are
    dependent (p.109)."""
    values = np.asarray(values, float)
    if values.size == 0:
        return float("nan"), float("nan"), float("nan")
    ub = np.unique(blocks)
    sums = np.array([values[blocks == u].sum() for u in ub])
    cnts = np.array([(blocks == u).sum() for u in ub])
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(ub), size=(n_boot, len(ub)))
    means = sums[pick].sum(axis=1) / np.maximum(cnts[pick].sum(axis=1), 1)
    return float(values.mean()), float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def max_drawdown(pnl: np.ndarray) -> float:
    eq = np.cumsum(np.asarray(pnl, float))
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    return float((peak - eq).max()) if eq.size else 0.0
