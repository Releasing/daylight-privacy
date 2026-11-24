"""Incremental technical indicators (spec p.7, p.54, p.104).

Every indicator is causal and O(1) per update. ``value`` is ``None`` during warm-up; consumers must
treat ``None`` as missing (explicit missing flag), never as zero.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


class EMA:
    """Exponential moving average seeded with the simple average of the first ``n`` inputs."""

    def __init__(self, n: int):
        if n < 1:
            raise ValueError("n >= 1")
        self.n = n
        self.alpha = 2.0 / (n + 1.0)
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
        else:
            self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class RSI:
    """Wilder RSI."""

    def __init__(self, n: int = 14):
        self.n = n
        self.prev: float | None = None
        self.gains: list[float] = []
        self.losses: list[float] = []
        self.avg_gain: float | None = None
        self.avg_loss: float | None = None
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.prev is None:
            self.prev = x
            return None
        ch = x - self.prev
        self.prev = x
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if self.avg_gain is None:
            self.gains.append(g)
            self.losses.append(l)
            if len(self.gains) == self.n:
                self.avg_gain = sum(self.gains) / self.n
                self.avg_loss = sum(self.losses) / self.n
        else:
            self.avg_gain = (self.avg_gain * (self.n - 1) + g) / self.n
            self.avg_loss = (self.avg_loss * (self.n - 1) + l) / self.n  # type: ignore[operator]
        if self.avg_gain is not None:
            if self.avg_loss == 0:
                self.value = 100.0 if self.avg_gain > 0 else 50.0
            else:
                rs = self.avg_gain / self.avg_loss  # type: ignore[operator]
                self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class MACD:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast, self.slow, self.sig = EMA(fast), EMA(slow), EMA(signal)
        self.macd: float | None = None
        self.signal: float | None = None
        self.hist: float | None = None

    def update(self, x: float) -> float | None:
        f, s = self.fast.update(x), self.slow.update(x)
        if f is not None and s is not None:
            self.macd = f - s
            self.signal = self.sig.update(self.macd)
            self.hist = None if self.signal is None else self.macd - self.signal
        return self.macd


class ATR:
    """Wilder ATR from completed candles."""

    def __init__(self, n: int = 14):
        self.n = n
        self.prev_close: float | None = None
        self.trs: list[float] = []
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if high < low:
            raise ValueError("high < low")
        tr = high - low if self.prev_close is None else max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        if self.value is None:
            self.trs.append(tr)
            if len(self.trs) == self.n:
                self.value = sum(self.trs) / self.n
        else:
            self.value = (self.value * (self.n - 1) + tr) / self.n
        return self.value


class Bollinger:
    def __init__(self, n: int = 20, k: float = 2.0):
        self.n, self.k = n, k
        self.win: deque[float] = deque(maxlen=n)
        self.mid: float | None = None
        self.upper: float | None = None
        self.lower: float | None = None

    def update(self, x: float) -> tuple[float, float, float] | None:
        self.win.append(x)
        if len(self.win) < self.n:
            return None
        m = sum(self.win) / self.n
        sd = math.sqrt(sum((v - m) ** 2 for v in self.win) / self.n)
        self.mid, self.upper, self.lower = m, m + self.k * sd, m - self.k * sd
        return self.mid, self.upper, self.lower


class VWAP:
    """Session VWAP = Σ(p·v)/Σv. Zero volume → None (missing)."""

    def __init__(self) -> None:
        self.pv = 0.0
        self.v = 0.0

    def update(self, p: float, v: float) -> float | None:
        if v < 0:
            raise ValueError("negative volume")
        self.pv += p * v
        self.v += v
        return self.value

    @property
    def value(self) -> float | None:
        return self.pv / self.v if self.v > 0 else None

    def reset(self) -> None:
        self.pv = self.v = 0.0


class EWMAVar:
    """EWMA variance of log returns: var = λ·var + (1−λ)·r² (p.7) with a floor and jump flag."""

    def __init__(self, lam: float = 0.94, floor: float = 1e-10, jump_sigmas: float = 6.0):
        if not 0 < lam < 1:
            raise ValueError("lambda in (0,1)")
        self.lam, self.floor, self.jump_sigmas = lam, floor, jump_sigmas
        self.var: float | None = None
        self.last_jump = False

    def update(self, r: float) -> float:
        if self.var is None:
            self.var = max(r * r, self.floor)
            self.last_jump = False
        else:
            sd = math.sqrt(max(self.var, self.floor))
            self.last_jump = abs(r) > self.jump_sigmas * sd
            self.var = max(self.lam * self.var + (1 - self.lam) * r * r, self.floor)
        return self.var


@dataclass
class RollingReturns:
    """Price samples on a 1-second grid for log returns over 1/5/15/30/60/300 s (p.7)."""

    horizons: tuple[int, ...] = (1, 5, 15, 30, 60, 300)
    prices: deque = field(default_factory=lambda: deque(maxlen=301))
    last_sec: int | None = None

    def update(self, ts: float, price: float) -> None:
        sec = int(ts)
        if self.last_sec is not None and sec <= self.last_sec:
            if sec == self.last_sec and self.prices:
                self.prices[-1] = price
            return  # out-of-order: ignored (no retroactive change)
        if self.last_sec is not None:
            gap = sec - self.last_sec
            # carry forward last observed price across silent seconds (no fabricated moves)
            for _ in range(min(gap - 1, 300)):
                self.prices.append(self.prices[-1])
        self.prices.append(price)
        self.last_sec = sec

    def log_return(self, h: int) -> float | None:
        if len(self.prices) <= h:
            return None
        a, b = self.prices[-1 - h], self.prices[-1]
        return math.log(b / a) if a > 0 and b > 0 else None

    def all(self) -> dict[str, float | None]:
        return {f"ret_{h}s": self.log_return(h) for h in self.horizons}

    def realized_var(self, window: int = 60) -> float | None:
        n = min(window, len(self.prices) - 1)
        if n < 5:
            return None
        ps = list(self.prices)[-n - 1:]
        rs = [math.log(ps[i + 1] / ps[i]) for i in range(n) if ps[i] > 0 and ps[i + 1] > 0]
        return sum(r * r for r in rs) / len(rs) if rs else None

    def robust_trend(self, window: int = 60) -> float | None:
        """Theil–Sen slope (price per second) over the last ``window`` seconds."""
        ps = list(self.prices)[-window:]
        if len(ps) < 10:
            return None
        step = max(1, len(ps) // 30)
        pts = list(enumerate(ps))[::step]
        slopes = [(pj - pi) / (j - i) for a, (i, pi) in enumerate(pts) for (j, pj) in pts[a + 1:]]
        slopes.sort()
        return slopes[len(slopes) // 2] if slopes else None
