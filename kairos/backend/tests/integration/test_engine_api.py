"""End-to-end paper engine and API tests against an in-process fake Kalshi REST (TEST FIXTURES ONLY —
these values never reach a running deployment; the UI shows only live feed state)."""
import asyncio
import time
from decimal import Decimal

import pyotp
import pytest
from fastapi.testclient import TestClient

from kairos.api.app import create_app
from kairos.config import Settings
from kairos.db.schema import make_engine
from kairos.db.store import Store
from kairos.engine.core import Engine
from kairos.engine.market import MarketService
from kairos.feeds.base import Quote
from kairos.risk.mode import Mode
from kairos.security.vault import Vault, hash_password, new_totp_secret

RULES = ("If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) before 12:15 PM EST "
         "is at least the same average before 12:00 PM EST, then the market resolves to Yes.")


class FakeRest:
    def __init__(self, now):
        self.now = now
        self.book = {"orderbook_fp": {"yes_dollars": [["0.30", "500"]], "no_dollars": [["0.40", "500"]]}}  # YES ask 0.60
        self.result = None
        self.ticker = "KXBTC15M-26NOV241215"

    def _m(self, status="active"):
        return {"ticker": self.ticker, "event_ticker": self.ticker, "status": status,
                "open_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - 300)),
                "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now + 600)),
                "floor_strike": "100000.00", "rules_primary": RULES, "result": self.result or ""}

    async def series(self, t):
        return {"series": {"ticker": t, "fee_type": "quadratic", "fee_multiplier": 1}}

    async def exchange_status(self):
        return {"trading_active": True, "exchange_active": True}

    async def markets(self, **kw):
        return {"markets": [self._m()] if self.result is None else []}

    async def market(self, t):
        return {"market": self._m("settled" if self.result else "active")}

    async def orderbook(self, t, depth=None):
        return self.book

    async def trades(self, *a, **k):
        return {"trades": []}

    async def candlesticks(self, *a, **k):
        return {"candlesticks": []}

    async def close(self):
        pass


@pytest.fixture
def env(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path}/k.db", data_dir=str(tmp_path))
    store = Store(make_engine(s.database_url))
    vault = Vault(b"k" * 32)
    users = {}
    for name in ("owner", "tester"):
        secret = new_totp_secret()
        uid = store.create_user(name, hash_password("correct horse battery"), "x", "owner", Decimal("100"))
        from sqlalchemy import update

        from kairos.db import schema as S
        with store.engine.begin() as c:
            c.execute(update(S.users).where(S.users.c.id == uid).values(totp_secret_enc=vault.encrypt(secret.encode(), aad=f"totp:{uid}".encode())))
        users[name] = (uid, secret)
    rest = FakeRest(time.time())
    market = MarketService(s, store, None, rest)
    eng = Engine(s, store, market)
    eng.load_accounts()
    return s, store, vault, rest, market, eng, users


async def prime(market, rest, spot=100300.0, n=120):
    t0 = time.time() - n
    for i in range(n):
        await market.on_quote(Quote("coinbase", "BTC", spot - 1 + (i % 3), spot + 1 + (i % 3), None, t0 + i))
        await market.on_quote(Quote("kraken", "BTC", spot - 1, spot + 1, None, t0 + i))
    for a in ("ETH", "SOL"):
        await market.on_quote(Quote("kraken", a, 10, 10.1, None, time.time()))
    await market.load_series()
    await market.check_exchange()
    await market.discover()
    await market.poll_book()


async def test_rules_unverified_blocks_entries(env):
    s, store, vault, rest, market, eng, users = env
    await prime(market, rest)
    assert "rules_template_unverified" in market.market_reasons
    a = eng.accounts[users["owner"][0]]
    for _ in range(5):
        await eng.evaluate_all()
    assert not a.ledger.open_positions()
    assert any("rules_template_unverified" in r for d in a.decisions for r in d["reason_codes"])


async def test_paper_entry_settlement_and_restart(env):
    s, store, vault, rest, market, eng, users = env
    await prime(market, rest)
    from kairos.market.rules import parse_rule
    from kairos.kalshi.parse import parse_market
    market.verified_templates.add(parse_rule(parse_market(rest._m())).template)
    await market.discover()
    a = eng.accounts[users["owner"][0]]
    for _ in range(8):
        await market.poll_book()
        await eng.evaluate_all()
        await asyncio.sleep(0.01)
    snap = a.snapshot(market.view())
    assert snap["mode"] != "PAUSED", snap["halts"]
    pos = a.ledger.open_positions()
    assert pos, a.decisions[0]
    assert pos[0].side == "yes"  # spot well above strike with ~10 min left
    # never more than the risk gate allowed: worst-case loss ≤ 2% daily loss headroom on $100
    assert pos[0].cost <= Decimal("2.00")
    # paper fills never exceed displayed depth
    assert pos[0].qty <= 500
    cash_before = a.cash
    # official settlement YES
    rest.result = "yes"
    market.pending_settlement[rest.ticker] = market.active
    await market.check_settlements()
    assert not a.ledger.open_positions()
    assert a.cash > cash_before
    realized = a.funds.cumulative_realized
    # second settlement event is a no-op (exactly once)
    await a.on_settlement(rest.ticker, "yes")
    assert a.funds.cumulative_realized == realized
    # restart: reconstruct from durable records
    from kairos.engine.account import TradingAccount
    b = TradingAccount(user_id=a.user_id, settings=s, store=store)
    b.restore(Decimal("100"))
    assert b.funds.cumulative_realized == realized and b.cash == a.cash
    assert b.mode.current == Mode.PAUSED  # restart in paused state until evaluation/reconciliation


async def test_stale_feed_blocks_and_pause_preserves_exits(env):
    s, store, vault, rest, market, eng, users = env
    await prime(market, rest)
    a = eng.accounts[users["owner"][0]]
    v = market.view(now=time.time() + 60)  # composite now stale
    await a.evaluate(v)
    assert "feed_stale" in a.halts.codes()
    a.pause_entries("test")
    # seed a bot position and verify exit evaluation still runs while paused
    a.ledger.apply_fill("seed", rest.ticker, "yes", "buy", Decimal(5), Decimal("0.10"), Decimal(0), time.time(), "bot", "settlement")
    rest.book = {"orderbook_fp": {"yes_dollars": [["0.95", "500"]], "no_dollars": [["0.02", "500"]]}}
    await market.poll_book()
    await a.evaluate(market.view())
    assert any(d["action"] in ("exit", "hold") for d in a.decisions)


async def test_rollover_keeps_old_positions(env):
    s, store, vault, rest, market, eng, users = env
    await prime(market, rest)
    a = eng.accounts[users["owner"][0]]
    a.ledger.apply_fill("seed", rest.ticker, "yes", "buy", Decimal(2), Decimal("0.5"), Decimal(0), time.time(), "bot", "settlement")
    old = rest.ticker
    rest.ticker = "KXBTC15M-26NOV241230"
    await market.discover()
    assert market.active.ticker == rest.ticker and old in market.pending_settlement
    assert a.ledger.positions[(old, "yes")].qty == 2  # still keyed by old ticker


def login(client, name, secret):
    return client.post("/api/auth/login", json={"username": name, "password": "correct horse battery", "totp": pyotp.TOTP(secret).now()})


def test_api_auth_csrf_tenant_and_community(env):
    s, store, vault, rest, market, eng, users = env
    app = create_app(eng, vault, dev=True)
    c = TestClient(app)
    assert c.get("/api/snapshot").status_code == 401  # unauthenticated
    assert c.post("/api/auth/login", json={"username": "owner", "password": "wrong password!!", "totp": "000000"}).status_code == 401
    r = login(c, "owner", users["owner"][1])
    assert r.status_code == 200
    csrf = r.json()["csrf"]
    snap = c.get("/api/snapshot")
    assert snap.status_code == 200 and snap.headers["cache-control"] == "no-store"
    body = snap.text
    assert "password" not in body and "totp" not in body and "private_key" not in body
    assert snap.json()["account"]["user_id"] == users["owner"][0]  # only own account
    assert c.post("/api/controls/pause").status_code == 403  # missing CSRF
    assert c.post("/api/controls/pause", headers={"x-csrf-token": csrf}).status_code == 200
    assert eng.accounts[users["owner"][0]].controls.entries_paused
    assert not eng.accounts[users["tester"][0]].controls.entries_paused  # tenant isolation
    assert c.post("/api/controls/flatten", json={"confirm": "FLATTEN"}, headers={"x-csrf-token": csrf}).status_code in (200, 428)
    # community: default private, opt-in, revocation
    lb = c.get("/api/community/leaderboard?period=all&mode=paper").json()
    assert lb["participants"] == 0 and lb["rows"] == []
    assert c.post("/api/community/opt-in", json={"enabled": True, "alias": "owner"}, headers={"x-csrf-token": csrf}).status_code == 400
    assert c.post("/api/community/opt-in", json={"enabled": True, "alias": "k-alpha"}, headers={"x-csrf-token": csrf}).status_code == 200
    lb = c.get("/api/community/leaderboard?period=all&mode=paper").json()
    assert lb["participants"] == 1 and lb["rows"][0]["alias"] == "k-alpha" and "balance" not in str(lb)
    assert c.get("/api/community/leaderboard?period=all&mode=live").json()["participants"] == 1  # live shows 0 trades, separate data
    assert c.get("/api/community/leaderboard?period=all&mode=live").json()["sample_trades"] == 0
    c.post("/api/community/opt-in", json={"enabled": False}, headers={"x-csrf-token": csrf})
    assert c.get("/api/community/leaderboard?period=all&mode=paper").json()["participants"] == 0
    # second tenant cannot see first tenant's orders
    c2 = TestClient(app)
    login(c2, "tester", users["tester"][1])
    assert c2.get("/api/snapshot").json()["account"]["user_id"] == users["tester"][0]
    # sessions revoked → 401
    c.post("/api/auth/logout", headers={"x-csrf-token": csrf})
    assert c.get("/api/snapshot").status_code == 401


def test_login_rate_limited(env):
    s, store, vault, rest, market, eng, users = env
    c = TestClient(create_app(eng, vault, dev=True))
    codes = [c.post("/api/auth/login", json={"username": "owner", "password": "wrong password!!", "totp": "000000"}).status_code for _ in range(7)]
    assert 429 in codes


def test_ws_requires_auth_and_origin(env):
    s, store, vault, rest, market, eng, users = env
    c = TestClient(create_app(eng, vault, dev=True))
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/stream", headers={"origin": "http://localhost:5173"}) as ws:
            ws.receive_text()
    login(c, "owner", users["owner"][1])
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/stream", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()
    with c.websocket_connect("/api/stream", headers={"origin": "http://localhost:5173"}) as ws:
        import json
        first = json.loads(ws.receive_text())
        assert first["kind"] == "snapshot" and first["seq"] == 0


async def test_manual_and_bot_share_funds(env):
    s, store, vault, rest, market, eng, users = env
    await prime(market, rest)
    a = eng.accounts[users["owner"][0]]
    a.mode.current = Mode.CONSERVATIVE
    v = market.view()
    res = await a.manual_order(v, v.book, rest.ticker, "yes", "buy", 10, Decimal("0.60"), v.fees)
    assert res["ok"], res
    spend_after = a.account_state(time.time()).spendable_cash
    assert spend_after < Decimal("100")
    huge = await a.manual_order(v, v.book, rest.ticker, "yes", "buy", 100000, Decimal("0.60"), v.fees)
    assert not huge["ok"]  # cannot exceed shared buying power
    # bot exit manager does not touch manual inventory unless enabled
    assert a.ledger.qty_by_origin(rest.ticker, "yes", "manual") == 10
