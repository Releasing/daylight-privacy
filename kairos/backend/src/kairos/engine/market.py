"""Shared market ingestion service (spec p.3, p.88, p.106, p.111, p.133–134).

One instance per server. Owns Kalshi discovery/rollover, contract books, contract trades, BTC/ETH/SOL
external feeds, candles, levels, final-minute benchmark proxy sampling, settlement detection, fee schedule
monitoring and the raw recorder. All published values come from verified feed state; when a feed is
missing or stale the value is ``None`` and flagged — never synthesized.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..feeds.base import Quote, Trade
from ..feeds.composite import Composite
from ..features.candles import MultiTimeframe
from ..features.crossasset import CrossAsset
from ..features.flow import TradeFlow, book_features
from ..features.indicators import EWMAVar, RollingReturns
from ..integrity.health import HealthReport, LiquidityIntegrity, evaluate as evaluate_health
from ..kalshi.parse import MarketMeta, ParseError, parse_delta, parse_market, parse_orderbook, parse_trade
from ..kalshi.rest import KalshiHTTPError, KalshiREST
from ..levels.detect import LevelEngine
from ..levels.patterns import detect as detect_patterns
from ..market.book import OrderBook
from ..market.discovery import eligibility, select_windows
from ..market.rules import FinalMinuteSampler, SettlementRule, WindowClock
from ..models.horizons import forecast
from ..models.settlement import BasisTracker, SettlementEstimate, SettlementInputs, SettlementModel
from ..money import FeeSchedule
from ..storage.recorder import Recorder
from ..supervisor.latency import LatencyStats

log = logging.getLogger("kairos.market")
Listener = Callable[[str, dict], Awaitable[None]]


@dataclass
class ContractPoint:
    t: int
    yes_bid: float | None
    yes_ask: float | None
    last: float | None


@dataclass
class MarketView:
    """Immutable-ish snapshot handed to trading accounts each evaluation."""

    now: float
    meta: MarketMeta | None
    rule: SettlementRule | None
    clock: WindowClock | None
    book: OrderBook | None
    fees: FeeSchedule
    health: HealthReport
    market_reasons: list[str]
    spot: float | None
    sigma_log_per_s: float | None
    drift_log_per_s: float
    estimate: SettlementEstimate | None
    features: dict[str, Any]
    obs_sum: float
    n_obs: int
    basis_sd: float
    version: int
    exchange_open: bool | None


class MarketService:
    def __init__(self, settings: Settings, store, recorder: Recorder | None, rest: KalshiREST, ws_factory=None):
        self.s = settings
        self.store = store
        self.recorder = recorder
        self.rest = rest
        self.ws_factory = ws_factory  # callable(on_message) → KalshiWS, or None (REST polling fallback)
        self.feature_recorder = None  # storage.recorder.FeatureRecorder (set by the server)
        self.ws = None
        self.series = settings.kalshi.series_ticker
        self.books: dict[str, OrderBook] = {}
        self.metas: dict[str, MarketMeta] = {}
        self.active: MarketMeta | None = None
        self.upcoming: list[MarketMeta] = []
        self.rule: SettlementRule | None = None
        self.market_reasons: list[str] = ["no_active_market"]
        self.fees = FeeSchedule("unknown", source="not_loaded")
        self.fee_version: str | None = None
        self.composite = Composite(max_age_ms=settings.risk.max_feed_age_ms)
        self.mtf = MultiTimeframe()
        self.levels = LevelEngine()
        self.cross = CrossAsset()
        self.flow = TradeFlow()
        self.returns = RollingReturns()
        self.ewma = EWMAVar(0.97)
        self.liquidity = LiquidityIntegrity()
        self.samplers: dict[str, FinalMinuteSampler] = {}
        self.contract_points: dict[str, deque] = {}
        self.contract_trades: deque = deque(maxlen=500)
        self.last_trade_ts: dict[str, int] = {}
        self.pending_settlement: dict[str, MarketMeta] = {}
        self.exchange_open: bool | None = None
        self.verified_templates: set[str] = set(store.kv_get("verified_rules_templates", []) or [])
        self.model = SettlementModel(n_paths=settings.strategies.mc_paths, seed=settings.strategies.mc_seed)
        self.basis = BasisTracker()
        self.latency = LatencyStats()
        self.listeners: list[Listener] = []
        self.version = 0
        self._estimate_cache: tuple[int, float, SettlementEstimate | None] = (-1, 0.0, None)
        self.patterns: deque = deque(maxlen=50)
        self.last_p: dict[str, float] = {}
        self.events: deque = deque(maxlen=200)
        self.started = time.time()
        self.feed_status: dict[str, Any] = {"kalshi": "starting", "coinbase": "starting", "kraken": "starting"}
        self._tasks: list[asyncio.Task] = []
        for tf in ("1m", "5m", "15m"):
            self.mtf.series[tf].on_close(self._make_level_hook(tf))

    # ---- listeners ---------------------------------------------------------------------------
    def subscribe(self, fn: Listener) -> None:
        self.listeners.append(fn)

    async def emit(self, kind: str, data: dict) -> None:
        self.events.append({"ts": time.time(), "kind": kind, **{k: v for k, v in data.items() if k in ("ticker", "reason", "result")}})
        for fn in list(self.listeners):
            try:
                await fn(kind, data)
            except Exception:  # a listener failure never stops ingestion
                log.exception("listener failed on %s", kind)

    # ---- external feeds (p.3) -------------------------------------------------------------------
    async def on_quote(self, q: Quote) -> None:
        self.composite.update(q)
        cp = self.composite.price(q.asset, q.receive_ts)
        if cp is None:
            return
        if q.asset == "BTC":
            prev = self.returns.prices[-1] if self.returns.prices else None
            sec_before = self.returns.last_sec
            self.returns.update(q.receive_ts, cp.value)
            if prev and self.returns.last_sec != sec_before and prev > 0:
                self.ewma.update(math.log(cp.value / prev))
            self._sample_final_minute(q.receive_ts, cp.value)
        self.cross.update(q.asset, q.receive_ts, cp.value)

    async def on_trade(self, t: Trade) -> None:
        if t.asset == "BTC":
            ts = t.exchange_ts or t.receive_ts
            closed = self.mtf.update(ts, t.price, t.size, t.side)
            self.flow.add(ts, t.size, t.side)
            if closed:
                self.version += 1

    async def on_raw(self, venue: str, msg: dict, rts: float) -> None:
        if self.recorder:
            self.recorder.record(venue, str(msg.get("channel") or msg.get("type") or ""), msg, rts)

    def _make_level_hook(self, tf: str):
        def hook(bar) -> None:
            s = self.mtf.series[tf]
            changed = self.levels.on_bar(s.completed(200), s.atr.value, int(bar.start + bar.tf))
            for lv in self.levels.nearest(bar.close, 2)["support"] + self.levels.nearest(bar.close, 2)["resistance"]:
                for ev in detect_patterns(s.completed(10), lv):
                    self.patterns.append({"tf": tf, **ev.to_wire()})
            if changed:
                self.version += 1
        return hook

    def _sample_final_minute(self, ts: float, value: float) -> None:
        m = self.active
        if m is None or m.close_time is None:
            return
        close = m.close_time.timestamp()
        avg = self.rule.averaging_seconds if self.rule else 60
        if close - avg <= ts < close:
            smp = self.samplers.setdefault(m.ticker, FinalMinuteSampler(window_start=close - avg, n_total=avg))
            smp.observe(ts, Decimal(str(round(value, 2))))

    # ---- Kalshi ---------------------------------------------------------------------------------
    async def load_series(self) -> None:
        try:
            data = await self.rest.series(self.series)
        except Exception as exc:
            log.warning("series fetch failed: %s", type(exc).__name__)
            return
        fs = FeeSchedule.from_series(data.get("series", data) if isinstance(data, dict) else {})
        if self.fee_version and fs.version != self.fee_version:
            await self.emit("fee_schedule_changed", {"old": self.fee_version, "new": fs.version})
        self.fees, self.fee_version = fs, fs.version

    async def discover(self) -> None:
        now = time.time()
        try:
            data = await self.rest.markets(series_ticker=self.series, status="open", limit=50)
        except Exception as exc:
            self.feed_status["kalshi"] = f"discovery_error:{type(exc).__name__}"
            self.market_reasons = ["discovery_failed"]
            return
        res = select_windows(data, self.series, now)
        for m in ([res.active] if res.active else []) + res.upcoming:
            self.metas[m.ticker] = m
            self.store.upsert_window(m.ticker, m.open_time.timestamp(), m.close_time.timestamp(), m.strike)
        prev = self.active
        if res.active and (prev is None or res.active.ticker != prev.ticker):
            await self._rollover(prev, res.active)
        elif res.active:
            self.active = res.active
        elif prev is not None and prev.close_time and now >= prev.close_time.timestamp():
            self.pending_settlement[prev.ticker] = prev
            self.active = None
        self.upcoming = res.upcoming
        if self.active:
            self.rule, self.market_reasons = eligibility(self.active, self.verified_templates)
        else:
            self.rule, self.market_reasons = None, ["no_active_market"]
        self.feed_status["kalshi"] = "ok"

    async def _rollover(self, old: MarketMeta | None, new: MarketMeta) -> None:
        """Atomic context switch; old positions stay keyed by the old ticker until settled (p.133)."""
        self.active = new
        self.books.setdefault(new.ticker, OrderBook(new.ticker))
        self.contract_points.setdefault(new.ticker, deque(maxlen=1200))
        if old is not None:
            self.pending_settlement[old.ticker] = old
        if self.ws is not None:
            await self.ws.set_tickers({new.ticker} | {m.ticker for m in self.upcoming[:1]})
        self.version += 1
        await self.emit("rollover", {"ticker": new.ticker, "previous": old.ticker if old else None})
        await self.backfill_contract(new)

    async def backfill_contract(self, m: MarketMeta) -> None:
        """Seed the contract chart from official candlesticks (1-minute)."""
        if m.open_time is None:
            return
        try:
            from ..kalshi.parse import parse_candles

            data = await self.rest.candlesticks(self.series, m.ticker, int(m.open_time.timestamp()), int(time.time()), 1)
            pts = self.contract_points.setdefault(m.ticker, deque(maxlen=1200))
            for c in parse_candles(data):
                pts.append(ContractPoint(int(c.ts.timestamp()), float(c.yes_bid_close) if c.yes_bid_close is not None else None,
                                         float(c.yes_ask_close) if c.yes_ask_close is not None else None,
                                         float(c.close) if c.close is not None else None))
        except Exception as exc:
            log.info("candlestick backfill unavailable: %s", type(exc).__name__)

    async def poll_book(self) -> None:
        m = self.active
        if m is None:
            return
        t0 = time.perf_counter()
        try:
            data = await self.rest.orderbook(m.ticker)
        except Exception as exc:
            b = self.books.setdefault(m.ticker, OrderBook(m.ticker))
            b.invalidate(f"rest_error:{type(exc).__name__}")
            return
        rts = time.time()
        self.latency.add("kalshi_rest_book_ms", (time.perf_counter() - t0) * 1000)
        if self.recorder:
            self.recorder.record("kalshi", "orderbook_rest", data, rts, m.ticker)
        try:
            snap = parse_orderbook(data)
        except ParseError as exc:
            self.books.setdefault(m.ticker, OrderBook(m.ticker)).invalidate(f"parse:{exc}")
            return
        b = self.books.setdefault(m.ticker, OrderBook(m.ticker))
        b.apply_snapshot(snap, None, rts)
        self.liquidity.observe(b)
        self.version += 1

    async def on_ws_message(self, msg: dict, recv_wall: float, recv_mono: int) -> None:
        typ = msg.get("type")
        body = msg.get("msg", {}) or {}
        ticker = body.get("market_ticker", "")
        if self.recorder:
            self.recorder.record("kalshi", str(typ), msg, recv_wall, ticker, msg.get("seq"))
        try:
            if typ == "orderbook_snapshot":
                b = self.books.setdefault(ticker, OrderBook(ticker))
                b.apply_snapshot(parse_orderbook(body), msg.get("seq"), recv_wall)
            elif typ == "orderbook_delta":
                b = self.books.setdefault(ticker, OrderBook(ticker))
                if not b.apply_delta(parse_delta(body), msg.get("seq"), recv_wall):
                    if self.ws is not None:
                        await self.ws.resync(ticker)  # snapshot rebuild after gap (p.6)
            elif typ == "trade":
                tp = parse_trade(body)
                self.contract_trades.append(tp)
                await self.emit("contract_trade", {"ticker": tp.ticker, "yes_price": str(tp.yes_price), "count": str(tp.count),
                                                   "ts": tp.ts.timestamp()})
            else:
                return
        except ParseError as exc:
            log.warning("kalshi ws parse error quarantined: %s", exc)
            return
        self.version += 1

    async def poll_trades(self) -> None:
        m = self.active
        if m is None:
            return
        try:
            data = await self.rest.trades(m.ticker, limit=100, min_ts=self.last_trade_ts.get(m.ticker))
        except Exception:
            return
        new = []
        for raw in data.get("trades", []) or []:
            try:
                tp = parse_trade(raw)
            except ParseError:
                continue
            if any(t.trade_id == tp.trade_id for t in self.contract_trades):
                continue
            new.append(tp)
        for tp in sorted(new, key=lambda t: t.ts):
            self.contract_trades.append(tp)
            self.last_trade_ts[m.ticker] = int(tp.ts.timestamp())
            await self.emit("contract_trade", {"ticker": tp.ticker, "yes_price": str(tp.yes_price), "count": str(tp.count),
                                               "ts": tp.ts.timestamp()})

    async def check_settlements(self) -> None:
        for ticker, meta in list(self.pending_settlement.items()):
            try:
                data = await self.rest.market(ticker)
            except Exception:
                continue
            m = parse_market(data.get("market", data))
            result = (m.result or "").lower()
            if result in ("yes", "no") and m.status in ("settled", "finalized", "determined", "closed"):
                smp = self.samplers.get(ticker)
                proxy_avg = float(smp.total / smp.n) if smp and smp.n else None
                exp_val = data.get("market", data).get("expiration_value")
                verified = True
                if proxy_avg is not None and exp_val not in (None, ""):
                    try:
                        self.basis.add(proxy_avg, float(exp_val))
                    except (TypeError, ValueError):
                        pass
                self.store.save_settlement(ticker, result, time.time(), verified, strike=meta.strike, proxy_avg=proxy_avg,
                                           model_p_yes=self.last_p.pop(ticker, None),
                                           expiration_value=str(exp_val) if exp_val not in (None, "") else None)
                del self.pending_settlement[ticker]
                await self.emit("settlement", {"ticker": ticker, "result": result, "expiration_value": exp_val,
                                               "proxy_avg": proxy_avg, "strike": str(meta.strike) if meta.strike is not None else None})

    async def check_exchange(self) -> None:
        try:
            st = await self.rest.exchange_status()
            self.exchange_open = bool(st.get("trading_active", st.get("exchange_active", False)))
        except Exception:
            self.exchange_open = None

    # ---- derived view -----------------------------------------------------------------------------
    def sigma(self) -> float | None:
        if self.ewma.var is None or len(self.returns.prices) < 30:
            return None
        return math.sqrt(self.ewma.var)

    def view(self, now: float | None = None) -> MarketView:
        now = now or time.time()
        m = self.active
        book = self.books.get(m.ticker) if m else None
        health = evaluate_health(book, self.composite, now, self.s.risk.max_quote_age_ms, self.s.risk.max_feed_age_ms,
                                 exchange_open=self.exchange_open)
        cp = self.composite.price("BTC", now)
        spot = cp.value if cp else None
        sig = self.sigma()
        trend = self.returns.robust_trend(60)
        drift = 0.0  # no-drift baseline (shrinkage); learned drift is a shadow challenger only
        smp = self.samplers.get(m.ticker) if m else None
        obs = {i: float(v) for i, v in smp.samples.items()} if smp else {}
        est = None
        if m is not None and spot is not None and sig is not None and m.strike is not None and self.rule is not None:
            ver, ts, cached = self._estimate_cache
            if ver == self.version and now - ts < 0.5:
                est = cached
            else:
                with self.latency.measure("lane_predictive"):
                    est = self.model.estimate(SettlementInputs(
                        spot=spot, strike=float(m.strike), now=now, close_ts=m.close_time.timestamp(), sigma_per_s=sig,
                        drift_per_s=drift, avg_seconds=self.rule.averaging_seconds, observed=obs,
                        comparator=self.rule.comparator, basis_mean=self.basis.mean, basis_sd=self.basis.sd))
                self._estimate_cache = (self.version, now, est)
                if est is not None and est.valid and m.close_time.timestamp() - now >= 60:
                    self.last_p[m.ticker] = est.p_yes  # last pre-final-minute forecast for calibration
        feats: dict[str, Any] = {}
        feats.update(self.returns.all())
        feats["realized_var_60"] = self.returns.realized_var(60)
        feats["trend_per_s"] = trend
        feats["ewma_sigma"] = sig
        feats["jump"] = self.ewma.last_jump
        feats.update(self.flow.features(now))
        feats.update(book_features(book))
        feats.update(self.mtf.features())
        feats.update(self.cross.features(now))
        atr = self.mtf.series["1m"].atr.value
        if spot is not None:
            feats.update(self.levels.features(spot, atr))
        feats["liquidity_penalty"] = self.liquidity.confidence_penalty()
        clock = WindowClock.from_meta(m) if m else None
        return MarketView(now, m, self.rule, clock, book, self.fees, health, list(self.market_reasons), spot, sig, drift, est,
                          feats, sum(obs.values()), len(obs), self.basis.sd, self.version, self.exchange_open)

    def chart_payload(self, ticker: str | None = None) -> dict:
        """Chart data for the UI: contract series (cents, executable bid/ask) and BTC reference ($)."""
        t = ticker or (self.active.ticker if self.active else None)
        pts = list(self.contract_points.get(t, [])) if t else []
        btc = self.mtf.series["1m"].completed(240)
        cur = self.mtf.series["1m"].current
        cp = self.composite.price("BTC", time.time())
        spot = cp.value if cp else None
        near = self.levels.nearest(spot, 2) if spot else {"support": [], "resistance": []}
        return {
            "ticker": t,
            "contract": [{"t": p.t, "bid": p.yes_bid, "ask": p.yes_ask, "last": p.last} for p in pts],
            "contract_trades": [{"t": tr.ts.timestamp(), "p": float(tr.yes_price), "q": float(tr.count)} for tr in list(self.contract_trades)[-200:]
                                if tr.ticker == t],
            "btc": [b.to_wire() for b in btc] + ([cur.to_wire()] if cur else []),
            "btc_source": {"label": "exchange_proxy", "venues": list(cp.venues) if cp else [], "note": "Coinbase/Kraken median — not the CF Benchmarks settlement index"},
            "strike": float(self.active.strike) if self.active and self.active.strike is not None else None,
            "levels": {"support": [l.to_wire() for l in near["support"]], "resistance": [l.to_wire() for l in near["resistance"]],
                       "all": [l.to_wire() for l in self.levels.levels if l.status != "stale"][:20]},
            "patterns": list(self.patterns)[-10:],
            "indicators": {k: self.mtf.series["1m"].features().get(k) for k in ("ema9", "ema21", "vwap", "bb_upper", "bb_lower", "bb_mid")},
        }

    def record_contract_point(self, now: float) -> None:
        m = self.active
        if m is None:
            return
        b = self.books.get(m.ticker)
        last = float(self.contract_trades[-1].yes_price) if self.contract_trades and self.contract_trades[-1].ticker == m.ticker else None
        yb = float(b.best_yes_bid()) if b and b.best_yes_bid() is not None else None
        ya = float(b.best_yes_ask()) if b and b.best_yes_ask() is not None else None
        pts = self.contract_points.setdefault(m.ticker, deque(maxlen=1200))
        sec = int(now)
        if pts and pts[-1].t == sec:
            pts[-1] = ContractPoint(sec, yb, ya, last)
        elif yb is not None or ya is not None:
            pts.append(ContractPoint(sec, yb, ya, last))

    def horizons(self, now: float) -> list[dict]:
        m = self.active
        left = (m.close_time.timestamp() - now) if m and m.close_time else 0
        return [h.__dict__ for h in forecast(0.0, self.sigma(), left)]  # no-drift baseline

    # ---- loops ---------------------------------------------------------------------------------------
    async def run(self, use_external: bool = True) -> None:
        from ..feeds.coinbase import CoinbaseFeed
        from ..feeds.kraken import KrakenFeed

        await self.load_series()
        await self.check_exchange()
        await self.discover()
        if use_external:
            self.coinbase = CoinbaseFeed(self.on_quote, self.on_trade, self.on_raw)
            self.kraken = KrakenFeed(self.on_quote, self.on_trade, self.on_raw)
            self._tasks += [asyncio.create_task(self.coinbase.run()), asyncio.create_task(self.kraken.run())]
        if self.ws_factory is not None:
            self.ws = self.ws_factory(self.on_ws_message)
            if self.active:
                await self.ws.set_tickers({self.active.ticker})
            self._tasks.append(asyncio.create_task(self.ws.run()))
        self._tasks += [asyncio.create_task(self._every(5.0, self.discover)),
                        asyncio.create_task(self._every(600.0, self.load_series)),
                        asyncio.create_task(self._every(30.0, self.check_exchange)),
                        asyncio.create_task(self._every(10.0, self.check_settlements))]
        if self.ws is None:
            self._tasks += [asyncio.create_task(self._every(self.s.kalshi.rest_poll_interval_s, self.poll_book)),
                            asyncio.create_task(self._every(2.0, self.poll_trades))]

    async def _every(self, period: float, fn) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("loop %s failed", getattr(fn, "__name__", fn))
            await asyncio.sleep(period)

    def tick(self, now: float) -> None:
        """1 Hz housekeeping: close candles on wall clock, record contract chart point."""
        if self.mtf.roll(now):
            self.version += 1
        self.record_contract_point(now)
        if self.feature_recorder is not None and self.active is not None:
            try:
                v = self.view(now)
                f = v.features
                if v.estimate is not None and v.estimate.valid and v.clock is not None:
                    z = None
                    if v.spot and v.sigma_log_per_s and v.meta and v.meta.strike is not None:
                        left = max(v.clock.close_ts - now - 40.0, 1.0)
                        z = (v.spot - float(v.meta.strike)) / (v.spot * v.sigma_log_per_s * left ** 0.5)
                    self.feature_recorder.add({"ts": now, "ticker": self.active.ticker, "seconds_left": v.clock.close_ts - now,
                                               "z_to_strike": z, "sigma": v.sigma_log_per_s, "ret_60s": f.get("ret_60s"),
                                               "ret_300s": f.get("ret_300s"), "flow_imb_15s": f.get("flow_imb_15s"),
                                               "k_spread": f.get("k_spread"), "k_depth_imb": f.get("k_depth_imb"),
                                               "eth_corr": f.get("eth_corr"), "sol_corr": f.get("sol_corr"),
                                               "p_model": v.estimate.p_yes, "spot_proxy": v.spot})
            except Exception:
                log.exception("feature record failed")
        cb = getattr(self, "coinbase", None)
        kr = getattr(self, "kraken", None)
        self.feed_status["coinbase"] = "ok" if cb and cb.connected else ("off" if cb is None else "reconnecting")
        self.feed_status["kraken"] = "ok" if kr and kr.connected else ("off" if kr is None else "reconnecting")
        if self.ws is not None:
            self.feed_status["kalshi_ws"] = "ok" if self.ws.connected else "reconnecting"

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.recorder:
            self.recorder.flush()
