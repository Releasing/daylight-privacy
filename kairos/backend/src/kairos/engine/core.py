"""Engine orchestration and authenticated delta streaming (spec p.88–89, p.106, p.134, p.142).

* One shared MarketService; one TradingAccount per tenant.
* Event-driven, coalesced evaluation (default 500 ms; bounded 250–1000 ms, p.56). Exits and risk run in
  the account's fast path before new entries; research never blocks this loop.
* Per-user stream: initial snapshot + sequenced deltas; clients that detect a gap request a snapshot.
  UI publishing is throttled (250 ms) independently of trading decisions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..money import D
from .account import TradingAccount
from .market import MarketService

log = logging.getLogger("kairos.engine")


class StreamHub:
    """Per-user fan-out with monotonically increasing sequence numbers per connection."""

    def __init__(self, max_queue: int = 500):
        self.conns: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self.max_queue = max_queue

    def connect(self, user_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue)
        self.conns[user_id].add(q)
        return q

    def disconnect(self, user_id: int, q: asyncio.Queue) -> None:
        self.conns[user_id].discard(q)

    async def publish(self, user_id: int | None, kind: str, data: Any) -> None:
        targets = [q for uid, qs in self.conns.items() if user_id is None or uid == user_id for q in qs]
        for q in targets:
            if q.full():
                # slow consumer: drop its queue contents and force a resnapshot (never silently skip seq)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"kind": "resync_required"})
            else:
                q.put_nowait({"kind": kind, "data": data})

    @property
    def clients(self) -> int:
        return sum(len(v) for v in self.conns.values())


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def dumps(o: Any) -> str:
    return json.dumps(o, default=_json_default, separators=(",", ":"))


class Engine:
    def __init__(self, settings: Settings, store, market: MarketService, eval_interval_s: float = 0.5):
        self.s = settings
        self.store = store
        self.market = market
        self.hub = StreamHub()
        self.accounts: dict[int, TradingAccount] = {}
        self.eval_interval = min(1.0, max(0.25, eval_interval_s))
        self._tasks: list[asyncio.Task] = []
        self.started = time.time()
        self.market.subscribe(self._on_market_event)
        self.server_time_offset = 0.0
        self.fixture_label: str | None = None  # set only by the offline layout-test harness

    def load_accounts(self, live_adapter_factory=None) -> None:
        for u in self.store.users():
            self.add_account(u, live_adapter_factory)

    def add_account(self, u: dict, live_adapter_factory=None) -> TradingAccount:
        paper = self.s.is_paper or self.store.live_authorization(u["id"]) is None
        acct = TradingAccount(user_id=u["id"], settings=self.s, store=self.store, paper=paper,
                              bot_manages_manual=bool(u.get("bot_manages_manual")), publish=self.hub.publish,
                              live_adapter=None if paper or live_adapter_factory is None else live_adapter_factory(u["id"]),
                              latency=self.market.latency)
        acct.restore(D(u["paper_start_equity"]))
        self.accounts[u["id"]] = acct
        return acct

    async def _on_market_event(self, kind: str, data: dict) -> None:
        if kind == "settlement":
            for a in self.accounts.values():
                await a.on_settlement(data["ticker"], data["result"])
        elif kind in ("fee_schedule_changed",):
            for a in self.accounts.values():
                a.halts.trigger("fee_schedule_changed", time.time(), dumps(data))
        await self.hub.publish(None, "market_event", {"kind": kind, **{k: v for k, v in data.items() if k != "raw"}})

    async def evaluate_all(self) -> None:
        v = self.market.view()
        with self.market.latency.measure("evaluation_cycle_ms"):
            for a in list(self.accounts.values()):
                try:
                    await a.evaluate(v)
                except Exception:
                    log.exception("account %s evaluation failed", a.user_id)
                    a.halts.trigger("write_failure", time.time(), "evaluation exception")
        if self.market.active is not None:
            self.store.upsert_window(self.market.active.ticker, v.clock.open_ts if v.clock else 0, v.clock.close_ts if v.clock else 0,
                                     self.market.active.strike, analyzed=True)

    async def _eval_loop(self) -> None:
        while True:
            t0 = time.perf_counter()
            try:
                await self.evaluate_all()
            except Exception:
                log.exception("evaluation loop error")
            await asyncio.sleep(max(0.0, self.eval_interval - (time.perf_counter() - t0)))

    async def _tick_loop(self) -> None:
        while True:
            now = time.time()
            self.market.tick(now)
            try:
                self.store.flush_decisions()
                if self.market.recorder:
                    self.market.recorder.flush()
            except Exception:
                log.exception("flush failed")
                for a in self.accounts.values():
                    a.halts.trigger("write_failure", now, "storage flush failed")
            await asyncio.sleep(1.0)

    async def _publish_loop(self) -> None:
        """Throttled UI deltas (250 ms) — never coupled to trading decisions."""
        last_chart = 0.0
        while True:
            if self.hub.clients:
                v = self.market.view()
                mk = self.market_wire(v)
                await self.hub.publish(None, "market", mk)
                for uid, a in self.accounts.items():
                    if self.hub.conns.get(uid):
                        await self.hub.publish(uid, "account", a.snapshot(v))
                if time.time() - last_chart > 2.0:
                    last_chart = time.time()
                    await self.hub.publish(None, "chart", self.market.chart_payload())
            await asyncio.sleep(0.25)

    def market_wire(self, v) -> dict:
        m = v.meta
        b = v.book
        est = v.estimate
        nxt = self.market.upcoming[0] if self.market.upcoming else None
        return {
            "server_time": time.time(),
            "ticker": m.ticker if m else None, "title": m.title if m else None, "subtitle": m.subtitle if m else None,
            "status": m.status if m else None, "strike": str(m.strike) if m and m.strike is not None else None,
            "open_ts": v.clock.open_ts if v.clock else None, "close_ts": v.clock.close_ts if v.clock else None,
            "next_ticker": nxt.ticker if nxt else None, "next_open_ts": nxt.open_time.timestamp() if nxt and nxt.open_time else None,
            "rules_hash": m.rules_hash if m else None, "rule": v.rule.__dict__ if v.rule else None,
            "market_reasons": v.market_reasons, "book": b.to_wire(10) if b else None,
            "book_age_ms": b.age_ms() if b else None,
            "quotes": {"yes_bid": _s(b.best_yes_bid()) if b else None, "yes_ask": _s(b.best_yes_ask()) if b else None,
                       "no_bid": _s(b.best_no_bid()) if b else None, "no_ask": _s(b.best_no_ask()) if b else None,
                       "last": str(self.market.contract_trades[-1].yes_price) if self.market.contract_trades and m and self.market.contract_trades[-1].ticker == m.ticker else None},
            "fees": {"type": v.fees.fee_type, "multiplier": str(v.fees.multiplier), "known": v.fees.known, "version": v.fees.version},
            "spot": v.spot, "spot_label": "exchange proxy (Coinbase/Kraken median)", "sigma_per_s": v.sigma_log_per_s,
            "estimate": est.to_wire() if est else None,
            "horizons": self.market.horizons(v.now),
            "final_minute": {"observed": v.n_obs, "sum": v.obs_sum, "basis_sd": v.basis_sd},
            "health": {"entry_blockers": v.health.entry_blockers, "warnings": v.health.warnings, "feed_age_ms": v.health.feed_age_ms,
                       "btc_only_fallback": v.health.btc_only_fallback},
            "feeds": self.market.feed_status, "exchange_open": v.exchange_open,
            "latency": self.market.latency.summary(), "patterns": list(self.market.patterns)[-5:],
            "features": {k: v.features.get(k) for k in ("ret_60s", "flow_imb_15s", "k_spread", "k_depth_imb", "1m_rsi14", "1m_atr14",
                                                         "eth_corr", "sol_corr", "dist_support_atr", "dist_resistance_atr")},
            "pending_settlement": list(self.market.pending_settlement),
            "version": v.version,
        }

    async def run(self, use_external: bool = True) -> None:
        await self.market.run(use_external=use_external)
        self._tasks += [asyncio.create_task(self._eval_loop()), asyncio.create_task(self._tick_loop()),
                        asyncio.create_task(self._publish_loop())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.market.stop()
        self.store.flush_decisions()


def _s(x) -> str | None:
    return None if x is None else str(x)
