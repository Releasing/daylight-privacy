"""Fault injection and replay determinism (spec p.5, p.15, p.23, p.24, p.41, p.92, p.98, p.99)."""
import asyncio
import time
from decimal import Decimal

import httpx
import pytest

from kairos.config import Settings
from kairos.engine.account import TradingAccount
from kairos.execution.orders import Journal
from kairos.kalshi.rest import KalshiREST, WriteOutcome
from kairos.ledger.lots import Ledger
from kairos.money import FeeSchedule
from kairos.replay.engine import BidPoint, counterfactual_exits, ledger_hash, path_label
from kairos.research.analyst import ClaudeBudget
from kairos.config import ResearchConfig

FEES = FeeSchedule("quadratic")


def rest_with(handler):
    transport = httpx.MockTransport(handler)
    return KalshiREST("https://api.example/trade-api/v2", signer=_signer(), client=httpx.AsyncClient(transport=transport), max_read_retries=2)


def _signer():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from kairos.kalshi.auth import KalshiSigner
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return KalshiSigner.from_pem("kid", k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


async def test_timeout_after_submission_is_unknown_not_retried():
    calls = []

    def h(req):
        calls.append(req.method)
        raise httpx.ReadTimeout("timeout", request=req)
    r = rest_with(h)
    res = await r.write("POST", "/portfolio/orders", {"x": 1})
    assert res.outcome == WriteOutcome.UNKNOWN and calls == ["POST"]  # never blind-retried


async def test_duplicate_client_id_409_and_429_and_401():
    codes = iter([409, 429, 401])

    def h(req):
        return httpx.Response(next(codes), json={"error": {"code": "x"}})
    r = rest_with(h)
    assert (await r.write("POST", "/portfolio/orders", {})).outcome == WriteOutcome.DUPLICATE
    assert (await r.write("POST", "/portfolio/orders", {})).outcome == WriteOutcome.REJECTED
    assert (await r.write("POST", "/portfolio/orders", {})).outcome == WriteOutcome.REJECTED


async def test_read_backoff_on_429_then_success():
    seq = iter([429, 429, 200])

    def h(req):
        code = next(seq)
        assert "KALSHI-ACCESS-SIGNATURE" not in req.headers  # public read is unauthenticated
        return httpx.Response(code, json={"ok": True})
    r = rest_with(h)
    assert await r.get("/markets") == {"ok": True}


async def test_signed_request_headers_and_path():
    seen = {}

    def h(req):
        seen.update(req.headers)
        seen["path"] = req.url.path
        return httpx.Response(200, json={"balance": 1})
    r = rest_with(h)
    await r.balance()
    assert seen["path"] == "/trade-api/v2/portfolio/balance" and "kalshi-access-signature" in seen


def test_dropped_ack_and_duplicate_fill_events():
    j = Journal()
    from kairos.contracts import OrderIntent
    i = OrderIntent(1, "T", "yes", "buy", 5, Decimal("0.5"), "s", "bot", "r", Decimal(0), Decimal(3))
    o = j.record(i)
    j.transition(o, "submitted")
    j.transition(o, "unknown")  # ack dropped
    j.apply_fill(o, Decimal(2), Decimal("0.5"), Decimal("0.02"))  # late fill reconciles from unknown
    assert o.state == "partial" and o.filled == 2
    L = Ledger()
    for _ in range(3):
        L.apply_fill("same-fill", "T", "yes", "buy", Decimal(2), Decimal("0.5"), Decimal(0), 0)
    assert L.pos("T", "yes").qty == 2  # duplicates never create exposure


async def test_close_during_pending_exit_settles_exactly_once(tmp_path):
    from kairos.db.schema import make_engine
    from kairos.db.store import Store
    s = Settings(database_url=f"sqlite:///{tmp_path}/f.db")
    store = Store(make_engine(s.database_url))
    from kairos.security.vault import hash_password
    uid = store.create_user("u", hash_password("long enough password"), "x", "owner", Decimal("100"))
    a = TradingAccount(user_id=uid, settings=s, store=store)
    a.restore(Decimal("100"))
    a.ledger.apply_fill("f1", "T", "yes", "buy", Decimal(10), Decimal("0.4"), Decimal(0), time.time(), "bot", "settlement")
    assert a.ledger.reserve_sell("T", "yes", Decimal(10))  # pending exit
    await a.on_settlement("T", "no")
    await a.on_settlement("T", "no")
    assert a.ledger.pos("T", "yes").qty == 0
    assert a.funds.cumulative_realized == Decimal("-4.0")


def test_path_labels_and_counterfactuals():
    path = [BidPoint(0, Decimal("0.40"), Decimal(100)), BidPoint(1, Decimal("0.45"), Decimal(100)),
            BidPoint(2, Decimal("0.61"), Decimal(20)), BidPoint(2.5, Decimal("0.60"), Decimal(30)), BidPoint(3, Decimal("0.30"), Decimal(5))]
    lab = path_label(Decimal("0.40"), 0, Decimal(50), path, Decimal("0.20"), Decimal("0.10"), cutoff_t=10)
    assert lab.outcome == "target" and lab.filled_qty == 30 and lab.remaining_qty == 20  # quantity-aware
    mid_touch = [BidPoint(0, Decimal("0.55"), Decimal(10))]  # midpoint may be 0.60 but bid is not
    assert path_label(Decimal("0.40"), 0, Decimal(5), mid_touch, Decimal("0.20"), Decimal("0.10"), 10).outcome == "timeout"
    never = [BidPoint(0, Decimal("0.38"), Decimal(10)), BidPoint(5, Decimal("0.35"), Decimal(10))]
    assert path_label(Decimal("0.40"), 0, Decimal(5), never, Decimal("0.20"), Decimal("0.10"), 4).outcome == "timeout"
    cf = counterfactual_exits(Decimal("0.40"), 10, 2.0, path, FEES, Decimal(0))
    assert "+1s" in cf and "oracle_best_exit_diagnostic_only" in cf and cf["hold_to_settlement"] == Decimal("-4.00")


def test_replay_deterministic_ledger_hash():
    def run():
        L = Ledger()
        for i, (a, px) in enumerate([("buy", "0.40"), ("buy", "0.35"), ("sell", "0.60")]):
            L.apply_fill(f"f{i}", "T", "yes", a, Decimal(10), Decimal(px), FEES.fee(10, px), i)
        return ledger_hash(L)
    assert run() == run()


def test_ai_unavailable_does_not_affect_trading():
    b = ClaudeBudget(ResearchConfig())
    with pytest.raises(Exception):
        b.estimate_max_cost(1000, 100)  # provider/pricing unavailable → research fails closed
    # trading code has no import dependency on the analyst module
    import kairos.engine.account as acct_mod
    assert "analyst" not in acct_mod.__dict__ and "anthropic" not in str(acct_mod.__dict__.keys())


def test_disk_full_triggers_write_failure_halt(tmp_path, monkeypatch):
    from kairos.storage.recorder import Recorder, StorageError
    r = Recorder(tmp_path, flush_rows=1)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr("pyarrow.parquet.write_table", boom)
    with pytest.raises(StorageError):
        r.record("kalshi", "x", {}, 1764000000.0)
