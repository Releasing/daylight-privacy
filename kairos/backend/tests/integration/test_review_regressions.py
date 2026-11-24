"""Regression tests for the independent trading-core review findings."""
import time
import uuid
from decimal import Decimal as Dm

import pytest

from kairos.config import Settings
from kairos.contracts import Fill, OrderIntent
from kairos.db.schema import make_engine
from kairos.db.store import Store
from kairos.engine.account import TradingAccount
from kairos.kalshi.parse import parse_orderbook
from kairos.market.book import OrderBook
from kairos.security.vault import hash_password


def mk(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path}/r.db")
    store = Store(make_engine(s.database_url))
    uid = store.create_user("u", hash_password("long enough password"), "x", "owner", Dm("100"))
    a = TradingAccount(user_id=uid, settings=s, store=store)
    a.restore(Dm("100"))
    return s, store, uid, a


async def fill(a, ticker, side, action, q, px, fee="0"):
    i = OrderIntent(a.user_id, ticker, side, action, q, Dm(px), "scalp", "bot", "r", Dm(0), Dm(0), paper=True)
    o = a.journal.record(i)
    a.journal.transition(o, "submitted")
    a.journal.transition(o, "acknowledged")
    a._order_meta[i.client_order_id] = {"origin": "bot", "strategy": "scalp"}
    if action == "sell":
        assert a.ledger.reserve_sell(ticker, side, Dm(q))
    now = time.time()
    await a.on_fill(Fill(f"f-{uuid.uuid4().hex[:8]}", i.client_order_id, "p", ticker, side, action, Dm(q), Dm(px), Dm(fee), False, now, now, True), o)
    time.sleep(0.002)


async def test_netting_cash_conserved(tmp_path):
    _, _, _, a = mk(tmp_path)
    await fill(a, "T1", "yes", "buy", 10, "0.40")
    await fill(a, "T1", "no", "buy", 10, "0.50")
    assert a.cash == Dm("101") and a.funds.buckets.total == Dm("101") and not a.ledger.open_positions()


async def test_restore_reproduces_hwm_and_peak(tmp_path):
    s, store, uid, a = mk(tmp_path)
    await fill(a, "A", "yes", "buy", 10, "0.50")
    await a.on_settlement("A", "yes")  # +5
    time.sleep(0.002)
    await fill(a, "B", "yes", "buy", 10, "0.50")
    await fill(a, "B", "yes", "sell", 10, "0.01")  # −4.9
    b = TradingAccount(user_id=uid, settings=s, store=store)
    b.restore(Dm("100"))
    for attr in ("active", "savings", "reserve"):
        assert getattr(b.funds.buckets, attr) == getattr(a.funds.buckets, attr)
    assert b.funds.high_water == a.funds.high_water and b.cash == a.cash and b.peak_active == a.peak_active
    assert "ledger_mismatch" not in b.halts.codes()


async def test_oversell_fill_does_not_touch_cash_and_restore_survives(tmp_path):
    s, store, uid, a = mk(tmp_path)
    await fill(a, "C", "yes", "buy", 5, "0.50")
    cash = a.cash
    i = OrderIntent(a.user_id, "C", "yes", "sell", 10, Dm("0.5"), "scalp", "bot", "r", Dm(0), Dm(0), paper=True)
    o = a.journal.record(i)
    a.journal.transition(o, "submitted")
    a.journal.transition(o, "acknowledged")
    now = time.time()
    await a.on_fill(Fill("over", i.client_order_id, "p", "C", "yes", "sell", Dm(10), Dm("0.5"), Dm(0), False, now, now, True), o)
    assert a.cash == cash and "ledger_mismatch" in a.halts.codes() and a.ledger.pos("C", "yes").qty == 5
    b = TradingAccount(user_id=uid, settings=s, store=store)
    b.restore(Dm("100"))  # must not raise; latched halt instead
    assert "ledger_mismatch" in b.halts.codes() and b.ledger.pos("C", "yes").qty == 5


async def test_manual_order_uses_its_own_book(tmp_path):
    from kairos.money import FeeSchedule
    _, _, _, a = mk(tmp_path)
    from kairos.risk.mode import Mode
    a.mode.current = Mode.CONSERVATIVE
    other = OrderBook("OTHER-1")
    other.apply_snapshot(parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.10", "100"]], "no_dollars": [["0.85", "100"]]}}), None, time.time())

    class V:  # active market is a different ticker with a very different price
        now = time.time(); clock = None; meta = None
        book = OrderBook("ACTIVE-1")
    V.book.apply_snapshot(parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.80", "100"]], "no_dollars": [["0.15", "100"]]}}), None, time.time())
    res = await a.manual_order(V, other, "OTHER-1", "yes", "buy", 2, Dm("0.20"), FeeSchedule("quadratic"), close_ts=time.time() + 600)
    assert res["ok"], res
    assert a.ledger.pos("OTHER-1", "yes").qty == 2 and a.ledger.pos("OTHER-1", "yes").avg_cost < Dm("0.20")
    assert (await a.manual_order(V, other, "OTHER-1", "yes", "buy", 1, Dm("0.20"), FeeSchedule("quadratic")))["reasons"] == ["market_close_time_unknown"]


async def test_send_exception_releases_reservations(tmp_path):
    _, _, _, a = mk(tmp_path)

    async def boom(*_a, **_k):
        raise RuntimeError("publish down")
    a.publish = boom
    b = OrderBook("T")
    b.apply_snapshot(parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.40", "100"]], "no_dollars": [["0.55", "100"]]}}), None, time.time())
    i = OrderIntent(a.user_id, "T", "yes", "buy", 2, Dm("0.45"), "settlement", "bot", "r", Dm(0), Dm(1), paper=True)
    a.gate.reservations["k"] = __import__("kairos.risk.allocator", fromlist=["Reservation"]).Reservation("k", "T", Dm(1), Dm(1), "bot", "settlement")
    o = a.journal.record(i)
    a._order_meta[i.client_order_id] = {"reservation": "k", "strategy": "settlement", "origin": "bot"}
    a.paper_exchange.fees = __import__("kairos.money", fromlist=["FeeSchedule"]).FeeSchedule("quadratic")
    await a._send(o, b)
    assert o.terminal and "k" not in a.gate.reservations


async def test_live_cancel_requires_exchange_confirmation(tmp_path):
    from kairos.kalshi.rest import WriteOutcome, WriteResult
    _, _, _, a = mk(tmp_path)
    a.paper = False

    class LA:
        async def cancel(self, oid):
            return WriteResult(WriteOutcome.UNKNOWN, None, None, 1.0)
    a.live_adapter = LA()
    i = OrderIntent(a.user_id, "T", "yes", "buy", 2, Dm("0.45"), "settlement", "bot", "r", Dm(0), Dm(1), paper=False)
    o = a.journal.record(i)
    a.journal.transition(o, "submitted")
    a.journal.transition(o, "acknowledged")
    o.order_id = "ex-1"
    assert await a.cancel_pending_entries() == 0 and o.state == "acknowledged"  # not confirmed → stays open
