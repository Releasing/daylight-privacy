"""Causal multi-timeframe candle aggregation (spec p.54–55, p.104–105).

Completed candles are immutable once emitted. The current candle is exposed only as *provisional*.
Out-of-order events older than the current bucket are counted and ignored (they can never alter a
historical decision). Silent intervals produce no candle (gap) rather than fabricated bars.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .indicators import ATR, EMA, MACD, RSI, VWAP, Bollinger

TIMEFRAMES = {"5s": 5, "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


@dataclass
class Bar:
    start: int
    tf: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    n: int = 0
    provisional: bool = True

    def to_wire(self) -> dict:
        return {"t": self.start, "o": self.open, "h": self.high, "l": self.low, "c": self.close, "v": self.volume,
                "p": self.provisional}


class CandleSeries:
    def __init__(self, tf_seconds: int, maxlen: int = 2000):
        self.tf = tf_seconds
        self.bars: deque[Bar] = deque(maxlen=maxlen)
        self.current: Bar | None = None
        self.out_of_order = 0
        self.gaps = 0
        self.ema9, self.ema21 = EMA(9), EMA(21)
        self.rsi, self.macd, self.atr, self.bb = RSI(14), MACD(), ATR(14), Bollinger(20, 2)
        self.vwap = VWAP()
        self._listeners: list = []

    def on_close(self, fn) -> None:
        self._listeners.append(fn)

    def update(self, ts: float, price: float, size: float = 0.0, side: str | None = None) -> Bar | None:
        """Feed a tick. Returns the completed bar when a bucket closes."""
        b = int(ts // self.tf) * self.tf
        done: Bar | None = None
        if self.current is not None:
            if b < self.current.start:
                self.out_of_order += 1
                return None
            if b > self.current.start:
                done = self._close()
                if b > done.start + self.tf:
                    self.gaps += 1
        if self.current is None:
            self.current = Bar(b, self.tf, price, price, price, price)
        c = self.current
        c.high, c.low, c.close = max(c.high, price), min(c.low, price), price
        c.volume += size
        if side == "buy":
            c.buy_volume += size
        elif side == "sell":
            c.sell_volume += size
        c.n += 1
        return done

    def roll(self, now: float) -> Bar | None:
        """Close the current bar if wall-clock moved past its end (no new tick needed)."""
        if self.current is not None and now >= self.current.start + self.tf:
            return self._close()
        return None

    def _close(self) -> Bar:
        c = self.current
        assert c is not None
        c.provisional = False
        self.bars.append(c)
        self.current = None
        self.ema9.update(c.close)
        self.ema21.update(c.close)
        self.rsi.update(c.close)
        self.macd.update(c.close)
        self.atr.update(c.high, c.low, c.close)
        self.bb.update(c.close)
        if c.volume > 0:
            self.vwap.update((c.high + c.low + c.close) / 3.0, c.volume)
        for fn in self._listeners:
            fn(c)
        return c

    def completed(self, n: int | None = None) -> list[Bar]:
        bars = list(self.bars)
        return bars if n is None else bars[-n:]

    def features(self) -> dict[str, float | None]:
        last = self.bars[-1] if self.bars else None
        body = wick_ratio = None
        if last is not None and last.high > last.low:
            body = abs(last.close - last.open) / (last.high - last.low)
            wick_ratio = 1.0 - body
        return {
            "ema9": self.ema9.value, "ema21": self.ema21.value, "rsi14": self.rsi.value,
            "macd": self.macd.macd, "macd_signal": self.macd.signal, "macd_hist": self.macd.hist,
            "atr14": self.atr.value, "bb_mid": self.bb.mid, "bb_upper": self.bb.upper, "bb_lower": self.bb.lower,
            "vwap": self.vwap.value, "body_ratio": body, "wick_ratio": wick_ratio,
        }


class MultiTimeframe:
    def __init__(self, tfs: dict[str, int] | None = None):
        self.series = {k: CandleSeries(v) for k, v in (tfs or TIMEFRAMES).items()}

    def update(self, ts: float, price: float, size: float = 0.0, side: str | None = None) -> dict[str, Bar]:
        closed = {}
        for k, s in self.series.items():
            b = s.update(ts, price, size, side)
            if b is not None:
                closed[k] = b
        return closed

    def roll(self, now: float) -> dict[str, Bar]:
        out = {}
        for k, s in self.series.items():
            b = s.roll(now)
            if b is not None:
                out[k] = b
        return out

    def features(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for k, s in self.series.items():
            for name, v in s.features().items():
                out[f"{k}_{name}"] = v
        return out
