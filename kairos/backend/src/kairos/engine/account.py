"""Per-tenant autonomous trading account (spec p.5, p.12, p.67–77, p.88, p.106, p.115, p.135).

Hot path (on every coalesced market evaluation):

    reconcile_if_needed → supervisor mode → manage existing positions (exits first)
    → if mode PAUSED or entries paused: record skips and stop
    → scan strategies on both sides → independent risk gate → reserve → submit → reconcile fills

Paper and live share the same proposal / intent schema. Paper fills are simulated against visible depth
after the measured latency. Live submission is reachable only through ``LiveAdapter`` after every
administrative gate passes.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..contracts import Candidate, Decision, Fill, OrderIntent, feature_hash
from ..execution.orders import ExecutionError, Journal, LiveAdapter, LiveTradingDisabled, Order, PaperExchange
from ..execution.reconcile import Markouts
from ..finance.accounts import FundsAccount
from ..ledger.lots import Ledger, LedgerError, Realization
from ..money import ZERO, D, FeeSchedule
from ..risk.allocator import AccountState, RiskGate
from ..risk.halts import HaltBook
from ..risk.mode import Mode, ModeController
from ..strategy.optimal_exit import evaluate_exit
from ..strategy.registry import EdgeRegistry
from ..strategy.strategies import MarketContext, arbitrage_candidate, rank, scalp_candidates, settlement_candidates
from .market import MarketView

log = logging.getLogger("kairos.account")


@dataclass
class Controls:
    entries_paused: bool = False
    paused_reason: str = ""
    read_only: bool = False


@dataclass
class TradingAccount:
    user_id: int
    settings: Settings
    store: Any
    paper: bool = True
    bot_manages_manual: bool = False
    publish: Any = None  # async callable(user_id, kind, data)
    live_adapter: LiveAdapter | None = None
    latency: Any = None  # shared LatencyStats
    ledger: Ledger = field(default_factory=Ledger)
    funds: FundsAccount = field(default_factory=FundsAccount)
    cash: Decimal = ZERO  # reconciled cash (paper: internally reconciled ledger cash)
    controls: Controls = field(default_factory=Controls)
    halts: HaltBook = field(default_factory=HaltBook)
    registry: EdgeRegistry = field(default_factory=EdgeRegistry)
    markouts: Markouts = field(default_factory=Markouts)
    decisions: deque = field(default_factory=lambda: deque(maxlen=200))
    reconciled: bool = False
    peak_active: Decimal = ZERO
    last_action: dict = field(default_factory=dict)
    _skip_throttle: dict = field(default_factory=dict)
    _order_meta: dict = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        p = self.settings.risk
        self.gate = RiskGate(p, paper=self.paper)
        self.mode = ModeController(dwell_s=p.mode_upgrade_dwell_seconds, required_obs=p.mode_upgrade_observations,
                                   aggressive_enabled=p.aggressive_enabled)
        self.journal = Journal(persist=lambda o: self.store.save_order(self.user_id, o))
        self.paper_exchange = PaperExchange(FeeSchedule("unknown"))
        for sid, ver in (("settlement", "settlement_mc_v1"), ("scalp", "scalp_fp_v1"), ("arbitrage", "arb_identity_v1")):
            self.registry.register(sid, ver)

    # ---- bootstrap / restart recovery (p.18, p.41, p.77) ----------------------------------------------
    def restore(self, start_equity: Decimal) -> None:
        """Rebuild state from authoritative durable records. Starts PAUSED until reconciliation."""
        events = self.store.ledger_for(self.user_id, self.paper)
        if not events and self.paper:
            self.store.append_ledger(self.user_id, f"u{self.user_id}-paper-deposit-0", "deposit", time.time(), True,
                                     self.settings.policy_version, price=start_equity, bucket_delta={"active": str(start_equity)})
            events = self.store.ledger_for(self.user_id, self.paper)
        # Replay deposits, fills and settlement markers in one chronological stream so high-water-mark
        # distributions reproduce the live run exactly (p.70, p.77).
        stream: list[tuple[float, int, str, dict]] = []
        for e in events:
            if e["event_type"] in ("deposit", "settle_marker"):
                stream.append((e["ts"], 0 if e["event_type"] == "deposit" else 2, e["event_type"], e))
        for f in self.store.fills_for(self.user_id, self.paper, limit=None):
            stream.append((f["receive_ts"], 1, "fill", f))  # local processing order (receipt), not exchange time
        stream.sort(key=lambda x: (x[0], x[1]))
        for _, _, kind, row in stream:
            try:
                if kind == "deposit":
                    amt = D(row["price"])
                    self.funds.deposit(row["event_id"], amt, row["ts"])
                    self.cash += amt
                elif kind == "fill":
                    self._apply_fill_to_books(row["fill_id"], row["ticker"], row["side"], row["action"], D(row["quantity"]), D(row["price"]),
                                              D(row["fee"]), row["exchange_ts"], row["origin"], row["strategy"], persist=False)
                else:
                    self._apply_settlement(row["market_id"], row["bucket_delta"]["result"], row["ts"], persist=False)
            except LedgerError as exc:
                self.halts.trigger("ledger_mismatch", time.time(), f"restore: {exc}")  # latched; account still loads
        saved = self.store.funds(self.user_id, self.paper)
        if saved and (saved.get("savings") != str(self.funds.buckets.savings) or saved.get("reserve") != str(self.funds.buckets.reserve)):
            if Decimal(saved.get("savings", "0")) != self.funds.buckets.savings or Decimal(saved.get("reserve", "0")) != self.funds.buckets.reserve:
                self.halts.trigger("ledger_mismatch", time.time(), "restored buckets differ from materialized funds_state")
        # orders open at crash: paper IOC orders cannot rest → mark canceled; live → must reconcile with exchange
        for row in self.store.orders_for(self.user_id, self.paper, open_only=True):
            if self.paper:
                row_state = row["state"]
                if row_state not in ("filled", "canceled", "rejected", "expired"):
                    from sqlalchemy import update

                    from ..db import schema as S
                    with self.store.engine.begin() as c:
                        c.execute(update(S.orders).where(S.orders.c.client_order_id == row["client_order_id"]).values(
                            state="canceled", reject_reason="restart_paper_ioc", updated_ts=time.time()))
            else:
                self.halts.trigger("reconnect", time.time(), "open live orders at restart")
        self.peak_active = max(self.peak_active, self.funds.buckets.active)  # keep the replayed peak (drawdown limit)
        self.reconciled = self.paper  # paper ledger is the authoritative record; live reconciles via exchange

    # ---- account state for the gate ---------------------------------------------------------------------
    def _realized_today(self, now: float) -> Decimal:
        day0 = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return sum((r.realized for r in self.ledger.realizations if r.ts >= day0), ZERO)

    def account_state(self, now: float) -> AccountState:
        open_orders = self.journal.open_orders()
        exp = self.ledger.worst_case_exposure()
        per_mkt: dict[str, int] = {}
        turnover: dict[str, Decimal] = {}
        for o in self.journal.orders.values():
            per_mkt[o.intent.ticker] = per_mkt.get(o.intent.ticker, 0) + 1
            turnover[o.intent.ticker] = turnover.get(o.intent.ticker, ZERO) + o.filled_cost
        spend = self.funds.spendable(self.cash, ZERO)
        return AccountState(active_equity=max(self.funds.buckets.active, ZERO), spendable_cash=spend,
                            realized_pnl_today=self._realized_today(now), peak_equity=self.peak_active,
                            open_exposure_by_market=exp, open_positions=len({p.ticker for p in self.ledger.open_positions()}),
                            open_orders=len(open_orders), orders_by_market=per_mkt, turnover_by_market=turnover,
                            reconciled=self.reconciled and not self.journal.unknown(),
                            consecutive_rejects=self._consecutive_rejects())

    def _consecutive_rejects(self) -> int:
        n = 0
        for o in reversed(list(self.journal.orders.values())):
            if not o.terminal:
                continue
            if o.state != "rejected":
                break
            n += 1
        return n

    # ---- evaluation ------------------------------------------------------------------------------------
    def _auto_halts(self, v: MarketView) -> None:
        now = v.now
        blockers = set(v.health.entry_blockers)
        mapping = {"btc_proxy_stale": "feed_stale", "book_stale": "feed_stale", "clock_skew": "clock_drift",
                   "exchange_closed_or_maintenance": "exchange_maintenance"}
        active_codes = {mapping[b] for b in blockers if b in mapping}
        if any(b.startswith("book_invalid:seq_gap") for b in blockers):
            active_codes.add("seq_gap")
        for code in active_codes:
            self.halts.trigger(code, now, ",".join(sorted(blockers)))
        for code in ("feed_stale", "seq_gap", "clock_drift", "exchange_maintenance"):
            if code not in active_codes:
                self.halts.clear_auto(code, now, reconciled=self.reconciled)
        if not v.fees.known:
            self.halts.trigger("fee_schedule_unknown", now, v.fees.source)
        elif "fee_schedule_unknown" in self.halts.active and v.fees.known:
            # schedule became known after start-up: the latch exists to stop trading on *unknown* fees
            self.halts.acknowledge("fee_schedule_unknown", now, "system:fee_loaded", True, self.reconciled)
        self.halts.reset_daily(now)

    def _mode(self, v: MarketView, st: AccountState) -> Mode:
        vetoes = list(self.halts.codes())
        if not st.reconciled:
            vetoes.append("unreconciled")
        health = Mode.STANDARD if v.health.entries_allowed else Mode.PAUSED
        if v.health.warnings:
            health = min(health, Mode.CONSERVATIVE)
        market = Mode.STANDARD if not v.market_reasons else Mode.DEFENSIVE
        spread = v.book.spread_yes() if v.book else None
        execution = Mode.STANDARD if spread is not None and spread <= Decimal("0.05") else Mode.DEFENSIVE
        validated = any(r.status in ("active", "eligible") for r in self.registry.records.values())
        strategy = Mode.STANDARD if validated else Mode.CONSERVATIVE  # limited evidence → conservative (p.67)
        return self.mode.evaluate(v.now, health, market, execution, strategy, Mode.AGGRESSIVE, vetoes)

    def _ctx(self, v: MarketView) -> MarketContext | None:
        if v.meta is None or v.book is None or v.clock is None or v.meta.strike is None:
            return None
        f = v.features
        sig = v.sigma_log_per_s
        trend = f.get("trend_per_s")
        trend_sig = (trend * 60 / (v.spot * sig * 60 ** 0.5)) if (trend is not None and sig and v.spot) else None
        ema9, ema21 = f.get("1m_ema9"), f.get("1m_ema21")
        agree = [f.get(f"{tf}_ema9") > f.get(f"{tf}_ema21") for tf in ("1m", "5m", "15m")
                 if f.get(f"{tf}_ema9") is not None and f.get(f"{tf}_ema21") is not None]
        rv = f.get("realized_var_60")
        vol_regime = (rv ** 0.5 / sig) if (rv and sig) else None
        del ema9, ema21
        return MarketContext(
            ticker=v.meta.ticker, now=v.now, close_ts=v.clock.close_ts, strike=float(v.meta.strike), spot=v.spot,
            sigma_log_per_s=sig, drift_log_per_s=v.drift_log_per_s, book=v.book, fees=v.fees, estimate=v.estimate,
            obs_sum=v.obs_sum, n_obs=v.n_obs, basis_sd=v.basis_sd, trend_sigma_per_min=trend_sig,
            flow_imbalance=f.get("flow_imb_15s"), mtf_agreement=(sum(agree) / len(agree)) if agree else None,
            vol_regime=vol_regime, jump=bool(f.get("jump")), liquidity_penalty=float(f.get("liquidity_penalty") or 0.0),
            quote_age_ms=v.book.age_ms(), features_hash=feature_hash(f), reason_codes=list(v.market_reasons))

    async def evaluate(self, v: MarketView) -> None:
        async with self._lock:
            self._t0 = time.perf_counter()
            self._decision_recorded = False
            try:
                await self._evaluate(v)
            finally:
                self._record_decision_latency()

    def _record_decision_latency(self) -> None:
        """Local decision time (event → order intent), excluding simulated/real exchange latency (p.91)."""
        if not self._decision_recorded and self.latency is not None:
            self.latency.add("decision_ms", (time.perf_counter() - self._t0) * 1000.0)
            self._decision_recorded = True

    async def _evaluate(self, v: MarketView) -> None:
        if True:
            self.paper_exchange.fees = v.fees
            self._auto_halts(v)
            st = self.account_state(v.now)
            mode = self._mode(v, st)
            await self._manage_positions(v, mode)
            if v.meta is None:
                return
            ctx = self._ctx(v)
            if ctx is None:
                self._skip(v, "all", "-", ["market_context_incomplete"] + v.market_reasons, mode)
                return
            if self.controls.entries_paused or self.controls.read_only:
                self._skip(v, "all", "-", ["entries_paused_by_operator"], mode)
                return
            cands: list[Candidate] = []
            sw = self.settings.strategies
            with_ms = time.perf_counter()
            if sw.settlement_enabled:
                cands += settlement_candidates(ctx)
            if sw.scalp_enabled:
                cands += scalp_candidates(ctx, sw.scalp_target_cents, sw.scalp_stop_cents, sw.scalp_timeout_seconds,
                                          self.settings.risk.entry_cutoff_seconds, seed=sw.mc_seed)
            if sw.arbitrage_enabled:
                a = arbitrage_candidate(ctx, qty=10)
                if a is not None:
                    cands.append(a)
            self.last_action["scan_ms"] = (time.perf_counter() - with_ms) * 1000
            traded_tickers = {p.ticker for p in self.ledger.open_positions()}
            for c in rank(cands):
                if c.ticker in traded_tickers and not sw.staged_entries:
                    self._decide(v, c, "skip", 0, ["staged_entries_disabled"], mode)
                    continue
                hard = self.gate.hard_checks(c, self.account_state(v.now), mode, self.halts.codes(), v.health.entry_blockers,
                                             series_ok=c.ticker.split("-")[0] in self.settings.risk.allowed_series,
                                             market_reasons=[r for r in v.market_reasons], seconds_left=ctx.seconds_left,
                                             quote_age_ms=ctx.quote_age_ms)
                hard += [r for r in c.reason_codes if r in ("nonpositive_conservative_ev", "insufficient_depth", "no_net_arbitrage")
                         and r not in hard]
                key = f"res-{uuid.uuid4().hex[:12]}"
                ap = self.gate.approve(c, self.account_state(v.now), mode, v.book, v.fees, sorted(set(hard)), key)
                if not ap.approved:
                    self._decide(v, c, "skip", 0, ap.reasons, mode, size_cap=ap.budget)
                    continue
                if c.strategy_id == "arbitrage":
                    self.gate.release(key)
                    self._decide(v, c, "skip", 0, ["arbitrage_two_leg_execution_research_only"], mode)
                    continue
                self._record_decision_latency()
                await self._submit_entry(v, c, ap.qty, ap.all_in, key, mode)
                break  # one entry per evaluation; next evaluation recomputes with fresh evidence

    # ---- exits (p.12, p.22, p.85) --------------------------------------------------------------------
    async def _manage_positions(self, v: MarketView, mode: Mode) -> None:
        if v.book is None or v.meta is None:
            return
        if not v.book.valid or (v.book.age_ms() or 1e9) > self.settings.risk.max_quote_age_ms:
            return  # no exit decisions on stale or invalid quotes; positions stay tracked (p.48, p.86)
        for p in list(self.ledger.open_positions()):
            if p.ticker != v.meta.ticker:
                continue  # previous windows await official settlement
            managed = p.available if self.bot_manages_manual else min(p.available, self.ledger.qty_by_origin(p.ticker, p.side, "bot") - p.reserved_sell)
            qty = int(managed)
            if qty <= 0:
                continue
            est = v.estimate
            pc = None
            if est is not None and est.valid:
                pc = est.p_conservative_yes if p.side == "yes" else est.p_conservative_no
            strategy = p.lots[0].strategy if p.lots else ""
            scalp = strategy == "scalp"
            tgt = stop = None
            cutoff = False
            if scalp:
                avg = p.avg_cost or ZERO
                tgt = avg + Decimal(self.settings.strategies.scalp_target_cents) / 100
                stop = avg - Decimal(self.settings.strategies.scalp_stop_cents) / 100
                opened = p.lots[0].opened_ts if p.lots else v.now
                cutoff = (v.now - opened) >= self.settings.strategies.scalp_timeout_seconds or \
                         (v.clock and v.clock.close_ts - v.now <= self.settings.risk.entry_cutoff_seconds)
            risk_exit = "daily_loss_limit" in self.halts.codes() or "security_incident" in self.halts.codes()
            d = evaluate_exit(v.book, p.side, qty, pc, v.fees, risk_requires_exit=risk_exit,
                              model_stale=est is None or not est.valid, scalp_target=tgt, scalp_stop=stop,
                              past_cutoff=bool(cutoff), allow_partial=self.settings.strategies.partial_exits)
            if d.action in ("sell_all", "sell_part") and d.qty > 0 and d.limit_price is not None:
                await self._submit_exit(v, p.ticker, p.side, d.qty, d.limit_price, strategy or "exit", d.reason, mode)
            else:
                self._throttled_decision(v, Decision(v.now, self.user_id, p.ticker, strategy or "position", p.side, "hold", qty, None, None,
                                                     pc, None, None, None, [d.reason], "exit_v1", "", mode.name, self.paper), key=f"hold-{p.ticker}-{p.side}")

    # ---- order submission --------------------------------------------------------------------------
    async def _submit_entry(self, v: MarketView, c: Candidate, qty: int, all_in: Decimal, key: str, mode: Mode, origin: str = "bot",
                            book=None, fees: FeeSchedule | None = None) -> Order:
        fee = (fees or v.fees).fee(qty, c.limit_price)
        i = OrderIntent(self.user_id, c.ticker, c.side, "buy", qty, c.limit_price, c.strategy_id, origin,
                        f"{c.strategy_id}:{','.join(c.reason_codes[:3]) or 'positive_conservative_ev'}", fee, all_in,
                        model_version=c.model_version, paper=self.paper)
        o = self.journal.record(i)
        self._order_meta[i.client_order_id] = {"reservation": key, "strategy": c.strategy_id, "origin": origin}
        self._decide(v, c, "enter", qty, [f"approved:{c.strategy_id}"], mode, size_cap=all_in)
        await self._send(o, book if book is not None else v.book)
        return o

    async def _submit_exit(self, v: MarketView, ticker: str, side: str, qty: int, limit: Decimal, strategy: str, reason: str, mode: Mode,
                           origin: str = "bot", book=None) -> Order | None:
        if not self.ledger.reserve_sell(ticker, side, Decimal(qty)):
            return None  # overlapping exits cannot sell the same inventory
        i = OrderIntent(self.user_id, ticker, side, "sell", qty, limit, strategy, origin, reason[:64], ZERO, ZERO, paper=self.paper)
        o = self.journal.record(i)
        self._order_meta[i.client_order_id] = {"reservation": None, "strategy": strategy, "origin": origin, "sell_reserved": qty}
        self._decide_raw(v, Decision(v.now, self.user_id, ticker, strategy, side, "exit", qty, limit, None, None, None, None, None,
                                     [reason], "exit_v1", "", mode.name, self.paper))
        await self._send(o, book if book is not None else v.book)
        return o

    async def _send(self, o: Order, book) -> None:
        """Submit an order against *its own* market's book. Every exit path finalizes the order so cash and
        sell reservations can never leak (p.15, p.72)."""
        try:
            await self._send_inner(o, book)
        except BaseException as exc:
            if not o.terminal and o.state != "unknown":
                if self.paper or o.state in ("created",):
                    self.journal.transition(o, "rejected" if o.state == "created" else "canceled", f"error:{type(exc).__name__}")
                    self._finalize(o)
                else:
                    self.journal.transition(o, "unknown", f"error:{type(exc).__name__}")
                    self.halts.trigger("reconnect", time.time(), f"exception during submit {o.intent.client_order_id}")
            if not isinstance(exc, Exception):
                raise
            log.exception("order send failed")

    async def _send_inner(self, o: Order, book) -> None:
        self.journal.transition(o, "submitted")
        o.submit_mono = time.monotonic()
        if self.paper:
            # fill against the book as seen after the measured/assumed latency (p.15, p.92)
            await asyncio.sleep(self.paper_exchange.latency_ms / 1000.0)
            self.journal.transition(o, "acknowledged")
            fills = []
            if book is not None and book.valid and (book.age_ms() or 1e9) <= self.settings.risk.max_quote_age_ms:
                fills = self.paper_exchange.execute_ioc(o, book, time.time())
            for f in fills:
                await self.on_fill(f, o)
            if not o.terminal:
                self.journal.transition(o, "canceled", "ioc_remaining")
            self._finalize(o)
            return
        if self.live_adapter is None:
            self.journal.transition(o, "rejected", "live_adapter_missing")
            self._finalize(o)
            return
        try:
            res = await self.live_adapter.submit(o.intent)
        except LiveTradingDisabled as exc:
            self.journal.transition(o, "rejected", f"live_disabled:{exc}")
            self._finalize(o)
            return
        from ..kalshi.rest import WriteOutcome

        if res.outcome == WriteOutcome.ACCEPTED:
            body = res.body.get("order", res.body) if isinstance(res.body, dict) else {}
            o.order_id = str(body.get("order_id", ""))
            o.ack_latency_ms = res.latency_ms
            self.journal.transition(o, "acknowledged")
        elif res.outcome == WriteOutcome.REJECTED:
            self.journal.transition(o, "rejected", str(res.status))
            self._finalize(o)
        else:  # UNKNOWN / DUPLICATE → reconcile before anything else (p.15)
            self.journal.transition(o, "unknown", res.outcome.value)
            self.halts.trigger("reconnect", time.time(), f"ambiguous write {o.intent.client_order_id}")

    def _finalize(self, o: Order) -> None:
        meta = self._order_meta.get(o.intent.client_order_id, {})
        if meta.get("reservation"):
            self.gate.release(meta["reservation"], o.filled_cost + o.fees)
        if o.intent.action == "sell":
            unfilled = Decimal(o.intent.quantity) - o.filled
            if unfilled > 0:
                self.ledger.release_sell(o.intent.ticker, o.intent.side, unfilled)
        if self.publish:
            asyncio.ensure_future(self.publish(self.user_id, "order", o.to_wire()))

    # ---- fills / settlement / P&L --------------------------------------------------------------------
    def _apply_fill_to_books(self, fill_id: str, ticker: str, side: str, action: str, qty: Decimal, price: Decimal, fee: Decimal,
                             ts: float, origin: str, strategy: str, persist: bool = True, sell_reserved: bool = False) -> list[Realization]:
        if fill_id in self.ledger.seen_fills:
            return []
        reals = self.ledger.apply_fill(fill_id, ticker, side, action, qty, price, fee, ts, origin, strategy,
                                       sell_reservation=sell_reserved, prefer_origin=origin)  # raises before any mutation
        if action == "buy":
            self.cash -= qty * price + fee
        else:
            self.cash += qty * price - fee
        # netting: each YES+NO pair closed by a complementary buy is worth $1 immediately (venue netting)
        self.cash += sum((r.qty for r in reals if r.kind == "net"), ZERO)
        for n, r in enumerate(reals):
            self._realize(f"{fill_id}:{n}", r, persist)
        return reals

    def _realize(self, event_id: str, r: Realization, persist: bool) -> None:
        credits = self.funds.apply_realized(event_id, r.realized, r.ts)
        self.peak_active = max(self.peak_active, self.funds.buckets.active)
        if persist:
            pos = self.ledger.positions.get((r.ticker, r.side))
            closed = pos is not None and pos.qty == 0
            self.store.append_ledger(self.user_id, f"pnl-{event_id}", "fill" if r.kind != "settle" else "settle", r.ts, self.paper,
                                     self.settings.policy_version, market_id=r.ticker, quantity=r.qty, price=(r.proceeds / r.qty if r.qty else None),
                                     fee=r.exit_fee, realized_pnl=r.realized, strategy=r.strategy, origin=r.origin,
                                     episode=pos.episode_id if pos else None,
                                     bucket_delta={**{k: str(v) for k, v in credits.items()}, "episode_closed": closed})
            self.store.save_funds(self.user_id, self.paper, self.funds_wire())
            if credits and any(v > 0 for v in credits.values()) and self.publish:
                asyncio.ensure_future(self.publish(self.user_id, "notice", {"kind": "profit_allocation", "credits": {k: str(v) for k, v in credits.items()},
                                                                         "policy": self.funds.policy}))

    async def on_fill(self, f: Fill, o: Order) -> None:
        meta = self._order_meta.get(f.client_order_id, {})
        if not self.store.save_fill(self.user_id, f, meta.get("origin", o.intent.origin), meta.get("strategy", o.intent.strategy_id)):
            return  # duplicate fill
        self.journal.apply_fill(o, f.quantity, f.price, f.fee)
        try:
            self._apply_fill_to_books(f.fill_id, f.ticker, f.side, f.action, f.quantity, f.price, f.fee, f.exchange_ts,
                                      meta.get("origin", "bot"), meta.get("strategy", ""), sell_reserved=f.action == "sell")
        except LedgerError as exc:
            self.halts.trigger("ledger_mismatch", time.time(), str(exc))
        if meta.get("reservation") and f.action == "buy":
            self.gate.shrink(meta["reservation"], f.quantity * f.price + f.fee, f.quantity * f.price + f.fee)
        self.markouts.on_fill(meta.get("strategy", ""), f.side, f.action, float(f.price), f.exchange_ts, f.is_maker)
        self.last_action = {"ts": f.exchange_ts, "kind": f"{f.action}_{f.side}", "qty": str(f.quantity), "price": str(f.price),
                            "reason": o.intent.reason, "ticker": f.ticker}
        if self.publish:
            asyncio.ensure_future(self.publish(self.user_id, "fill", {"fill_id": f.fill_id, "ticker": f.ticker, "side": f.side, "action": f.action,
                                                      "qty": str(f.quantity), "price": str(f.price), "fee": str(f.fee), "ts": f.exchange_ts,
                                                      "paper": f.paper, "origin": meta.get("origin", "bot")}))

    def _apply_settlement(self, ticker: str, result: str, ts: float, persist: bool = True) -> list[Realization]:
        reals = self.ledger.settle(ticker, result, ts)
        for r in reals:
            self.cash += r.proceeds
            self._realize(f"settle-{ticker}-{r.side}", r, persist)
        return reals

    async def on_settlement(self, ticker: str, result: str) -> None:
        async with self._lock:
            if not any(p.ticker == ticker for p in self.ledger.open_positions()):
                return
            ts = time.time()
            if not self.store.append_ledger(self.user_id, f"u{self.user_id}-settle-{ticker}", "settle_marker", ts, self.paper,
                                            self.settings.policy_version, market_id=ticker, bucket_delta={"result": result}):
                return  # settlement reconciles exactly once
            reals = self._apply_settlement(ticker, result, ts)
            if self.publish:
                await self.publish(self.user_id, "settlement", {"ticker": ticker, "result": result,
                                                                "realized": str(sum((r.realized for r in reals), ZERO))})

    # ---- manual orders (p.135) ------------------------------------------------------------------------
    async def manual_order(self, v: MarketView, book, ticker: str, side: str, action: str, qty: int, limit: Decimal, fees: FeeSchedule,
                           close_ts: float | None = None) -> dict:
        async with self._lock:
            now = time.time()
            if book is None or not book.valid or book.ticker != ticker:
                return {"ok": False, "reasons": ["book_unavailable"]}
            if (book.age_ms() or 1e9) > self.settings.risk.max_quote_age_ms:
                return {"ok": False, "reasons": ["quote_stale"]}
            if not fees.known:
                return {"ok": False, "reasons": ["fee_schedule_unknown"]}
            self.paper_exchange.fees = fees
            if action == "sell":
                o = await self._submit_exit(v, ticker, side, qty, limit, "manual", "manual_sell", self.mode.current, origin="manual", book=book)
                return {"ok": o is not None, "order": o.to_wire() if o else None, "reasons": [] if o else ["insufficient_inventory"]}
            c = Candidate("manual", ticker, side, "buy", qty, limit, 0.0, 0.0, Decimal("0"), limit, None, {}, 0.0, 0, "manual", "",
                          [], hold_seconds=900)
            if v.clock and v.meta and v.meta.ticker == ticker:
                secs = v.clock.close_ts - now
            elif close_ts is not None:
                secs = close_ts - now
            else:
                return {"ok": False, "reasons": ["market_close_time_unknown"]}
            hard = self.gate.hard_checks(c, self.account_state(now), self.mode.current, [h for h in self.halts.codes() if h in
                                         ("security_incident", "ledger_mismatch", "write_failure")], [], True, [], secs, book.age_ms(), origin="manual")
            key = f"man-{uuid.uuid4().hex[:12]}"
            ap = self.gate.approve(c, self.account_state(now), self.mode.current, book, fees, hard, key, origin="manual", requested_qty=qty)
            if not ap.approved:
                self.gate.release(key)
                return {"ok": False, "reasons": ap.reasons, "caps": ap.caps}
            o = await self._submit_entry(v, c, ap.qty, ap.all_in, key, self.mode.current, origin="manual", book=book, fees=fees)
            self.store.audit(self.user_id, "manual_order", {"ticker": ticker, "side": side, "qty": qty, "limit": str(limit)})
            return {"ok": True, "order": o.to_wire()}

    # ---- controls (p.16, p.115) ----------------------------------------------------------------------
    def pause_entries(self, reason: str) -> None:
        self.controls.entries_paused, self.controls.paused_reason = True, reason

    def resume_entries(self) -> bool:
        if not self.reconciled or self.journal.unknown():
            return False
        self.controls.entries_paused, self.controls.paused_reason = False, ""
        return True

    async def cancel_pending_entries(self) -> int:
        async with self._lock:
            return await self._cancel_pending_entries()

    async def _cancel_pending_entries(self) -> int:
        """Paper: cancel locally. Live: cancel at the exchange first; only a confirmed cancel releases
        reservations (a late fill on a locally-canceled order would otherwise be lost)."""
        n = 0
        for o in self.journal.open_orders():
            if o.intent.action != "buy" or o.state not in ("created", "acknowledged", "resting"):
                continue
            if not self.paper:
                if self.live_adapter is None or not o.order_id:
                    continue
                from ..kalshi.rest import WriteOutcome
                try:
                    res = await self.live_adapter.cancel(o.order_id)
                except Exception:
                    continue
                if res.outcome != WriteOutcome.ACCEPTED:
                    continue
            self.journal.transition(o, "canceled", "operator_cancel_entries")
            self._finalize(o)
            n += 1
        return n

    async def emergency_flatten(self, v: MarketView) -> list[dict]:
        """Attempt to sell all bot+manual inventory at available bids. Reports actual results; liquidation is not
        guaranteed (p.16, p.115). Runs under the account lock so no entry can be mid-flight."""
        async with self._lock:
            out = []
            self.pause_entries("emergency_flatten")
            await self._cancel_pending_entries()
            if v.book is None or not v.book.valid:
                return [{"ok": False, "reason": "no_valid_book"}]
            for p in list(self.ledger.open_positions()):
                if v.meta is None or p.ticker != v.meta.ticker:
                    out.append({"ticker": p.ticker, "ok": False, "reason": "market_closed_awaiting_settlement"})
                    continue
                f = v.book.sell(p.side, p.available)
                if f.quantity <= 0 or f.worst is None:
                    out.append({"ticker": p.ticker, "side": p.side, "ok": False, "reason": "no_bids"})
                    continue
                o = await self._submit_exit(v, p.ticker, p.side, int(f.quantity), f.worst, "flatten", "emergency_flatten",
                                            self.mode.current, origin="manual")
                out.append({"ticker": p.ticker, "side": p.side, "ok": o is not None, "state": o.state if o else None,
                            "filled": str(o.filled) if o else "0"})
            return out

    # ---- decision records (p.108) ---------------------------------------------------------------------
    def _decide(self, v: MarketView, c: Candidate, action: str, qty: int, reasons: list[str], mode: Mode, size_cap: Decimal | None = None) -> None:
        d = Decision(v.now, self.user_id, c.ticker, c.strategy_id, c.side, action, qty, c.limit_price, c.p_point, c.p_conservative,
                     c.ev_per_contract, c.confidence, size_cap, reasons, c.model_version, c.feature_hash, mode.name, self.paper)
        if action == "skip":
            self._throttled_decision(v, d, key=f"{c.strategy_id}-{c.side}-{reasons[0] if reasons else ''}")
        else:
            self._decide_raw(v, d)
        self.last_candidates = getattr(self, "last_candidates", {})
        self.last_candidates[f"{c.strategy_id}:{c.side}"] = {**c.to_wire(), "decision": action, "reasons": reasons,
                                                             "size_cap": None if size_cap is None else str(size_cap)}

    def _skip(self, v: MarketView, strategy: str, side: str, reasons: list[str], mode: Mode) -> None:
        ticker = v.meta.ticker if v.meta else "-"
        self._throttled_decision(v, Decision(v.now, self.user_id, ticker, strategy, side, "skip", 0, None, None, None, None, None, None,
                                             reasons, "", "", mode.name, self.paper), key=f"skip-{reasons[0] if reasons else ''}")

    def _throttled_decision(self, v: MarketView, d: Decision, key: str, every_s: float = 5.0) -> None:
        last = self._skip_throttle.get(key, 0.0)
        if v.now - last >= every_s:  # sample routine events; material decisions always kept
            self._skip_throttle[key] = v.now
            self._decide_raw(v, d)

    def _decide_raw(self, v: MarketView, d: Decision) -> None:
        self.decisions.appendleft(d.to_wire())
        self.store.add_decision(d)

    # ---- wire views ---------------------------------------------------------------------------------
    def funds_wire(self) -> dict:
        b = self.funds.buckets
        return {"active": str(b.active), "savings": str(b.savings), "reserve": str(b.reserve), "total": str(b.total),
                "cumulative_realized": str(self.funds.cumulative_realized), "high_water": str(self.funds.high_water),
                "policy": self.funds.policy, "frozen": self.funds.frozen, "cash": str(self.cash)}

    def snapshot(self, v: MarketView | None) -> dict:
        now = time.time()
        st = self.account_state(now)
        marks = {}
        positions = []
        for p in self.ledger.open_positions():
            bid = None
            if v is not None and v.book is not None and v.meta is not None and p.ticker == v.meta.ticker:
                bid = v.book.best_yes_bid() if p.side == "yes" else v.book.best_no_bid()
            marks[(p.ticker, p.side)] = bid
            mark_val = (p.qty * bid - p.cost) if bid is not None else None
            positions.append({"ticker": p.ticker, "side": p.side, "qty": str(p.qty), "avg_cost": str(p.avg_cost.quantize(Decimal("0.0001"))) if p.avg_cost else None,
                              "cost": str(p.cost), "bid": str(bid) if bid is not None else None,
                              "unrealized_at_bid": str(mark_val.quantize(Decimal("0.01"))) if mark_val is not None else None,
                              "reserved_sell": str(p.reserved_sell), "origins": sorted({l.origin for l in p.lots}),
                              "strategy": p.lots[0].strategy if p.lots else ""})
        unreal = self.ledger.unrealized(marks)
        today = self._realized_today(now)
        day0 = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        closed_today = [e for e in self.ledger.closed_episodes if e["ts"] >= day0]
        wins = sum(1 for e in closed_today if e["realized"] > 0)
        losses = sum(1 for e in closed_today if e["realized"] < 0)
        return {
            "user_id": self.user_id, "paper": self.paper, "mode": self.mode.current.name, "mode_transitions": self.mode.transitions[-10:],
            "controls": self.controls.__dict__, "halts": [{"code": h.code, "kind": h.kind, "since": h.since, "detail": h.detail[:200]}
                                                          for h in self.halts.active.values()],
            "reconciled": st.reconciled, "funds": self.funds_wire(), "spendable": str(st.spendable_cash),
            "reserved_orders": str(self.gate.reserved_cash()),
            "risk": {"daily_loss_limit": str(st.active_equity * self.settings.risk.daily_loss_fraction),
                     "realized_today": str(today),
                     "loss_headroom": str(st.active_equity * self.settings.risk.daily_loss_fraction + min(today, ZERO)
                                         - sum(st.open_exposure_by_market.values(), ZERO)),
                     "market_cap_fraction": str(self.settings.risk.max_market_exposure_fraction),
                     "portfolio_exposure": str(sum(st.open_exposure_by_market.values(), ZERO)),
                     "policy_fingerprint": self.settings.risk.fingerprint()},
            "today": {"realized_net": str(today), "wins": wins, "losses": losses, "trades": len(closed_today)},
            "unrealized_at_bid": str(unreal.quantize(Decimal("0.01"))) if unreal is not None else None,
            "positions": positions, "open_orders": [o.to_wire() for o in self.journal.open_orders()],
            "recent_orders": [o.to_wire() for o in list(self.journal.orders.values())[-20:]][::-1],
            "decisions": list(self.decisions)[:40], "last_action": self.last_action,
            "candidates": getattr(self, "last_candidates", {}), "registry": {k: r.to_wire() for k, r in self.registry.records.items()},
            "markouts": self.markouts.summary(), "bot_manages_manual": self.bot_manages_manual,
        }
