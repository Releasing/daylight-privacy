"""Latency instrumentation and lane budgets (spec p.48, p.89, p.91, p.114)."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field

import numpy as np

BUDGET_MS = {"fast": 50.0, "predictive": 250.0, "deep": 5000.0}


@dataclass
class LatencyStats:
    window: int = 2000
    samples: dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=2000)))

    def add(self, metric: str, ms: float) -> None:
        self.samples[metric].append(ms)

    @contextmanager
    def measure(self, metric: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.add(metric, (time.perf_counter() - t0) * 1000.0)

    def pct(self, metric: str) -> dict[str, float | None]:
        v = self.samples.get(metric)
        if not v:
            return {"p50": None, "p95": None, "p99": None, "n": 0}
        a = np.fromiter(v, float)
        return {"p50": float(np.percentile(a, 50)), "p95": float(np.percentile(a, 95)),
                "p99": float(np.percentile(a, 99)), "n": int(a.size)}

    def summary(self) -> dict[str, dict]:
        return {k: self.pct(k) for k in sorted(self.samples)}

    def degraded(self, lane: str) -> bool:
        p = self.pct(f"lane_{lane}")
        return p["p95"] is not None and p["n"] >= 50 and p["p95"] > BUDGET_MS[lane]
