"""OFFLINE LAYOUT-TEST HARNESS — NOT A DATA SOURCE.

Runs the real Kairos engine + API against an in-process fake Kalshi REST and a seeded synthetic BTC random
walk so the interface can be exercised and screenshotted in a sandbox without exchange access. Every page
shows a full-width "LAYOUT TEST — FIXTURE DATA" banner. This module lives under tests/, is never installed
by the deploy scripts, and refuses to run with KAIROS_MODE=live.

    python tests/harness/fixture_server.py --static ../frontend/dist --port 8010
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))

from test_engine_api import RULES, FakeRest  # noqa: E402

from kairos.api.app import create_app  # noqa: E402
from kairos.config import Settings  # noqa: E402
from kairos.db.schema import make_engine  # noqa: E402
from kairos.db.store import Store  # noqa: E402
from kairos.engine.core import Engine  # noqa: E402
from kairos.engine.market import MarketService  # noqa: E402
from kairos.feeds.base import Quote, Trade  # noqa: E402
from kairos.kalshi.parse import parse_market  # noqa: E402
from kairos.market.rules import parse_rule  # noqa: E402
from kairos.security.vault import Vault, hash_password  # noqa: E402

TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # fixture-only account


class MovingRest(FakeRest):
    def __init__(self, now, market):
        super().__init__(now)
        self.market = market

    async def orderbook(self, t, depth=None):
        cp = self.market.composite.price("BTC", time.time())
        est = self.market._estimate_cache[2]
        p = est.p_yes if est is not None and est.valid else 0.5
        yb = max(0.01, min(0.97, round(p - 0.01, 2)))
        nb = max(0.01, min(0.97, round(1 - p - 0.01, 2)))
        rnd = random.Random(int(time.time()))
        return {"orderbook_fp": {"yes_dollars": [[f"{yb - i / 100:.2f}", str(rnd.randint(200, 4000))] for i in range(6) if yb - i / 100 > 0],
                                 "no_dollars": [[f"{nb - i / 100:.2f}", str(rnd.randint(200, 4000))] for i in range(6) if nb - i / 100 > 0]}}


async def synth(market, seed=7):
    rng = random.Random(seed)
    px = 97800.0
    t0 = time.time() - 3600
    # backfill one hour of 1 s synthetic BTC so candles/levels exist
    for i in range(3600):
        px *= math.exp(rng.gauss(0, 0.00012))
        ts = t0 + i
        await market.on_quote(Quote("coinbase", "BTC", px - 0.5, px + 0.5, ts, ts))
        await market.on_quote(Quote("kraken", "BTC", px - 0.7, px + 0.7, ts, ts))
        await market.on_trade(Trade("coinbase", "BTC", px, abs(rng.gauss(0.2, 0.15)), rng.choice(["buy", "sell"]), ts, ts))
        if i % 200 == 0:
            await asyncio.sleep(0)
    while True:
        px *= math.exp(rng.gauss(0, 0.00012))
        now = time.time()
        for v in ("coinbase", "kraken"):
            await market.on_quote(Quote(v, "BTC", px - 0.5, px + 0.5, now, now))
        for a, base in (("ETH", 3400.0), ("SOL", 180.0)):
            await market.on_quote(Quote("kraken", a, base * (px / 97800) - 0.1, base * (px / 97800) + 0.1, now, now))
        await market.on_trade(Trade("coinbase", "BTC", px, abs(rng.gauss(0.2, 0.15)), rng.choice(["buy", "sell"]), now, now))
        await asyncio.sleep(0.2)


def main():
    if os.environ.get("KAIROS_MODE") == "live":
        sys.exit("fixture harness refuses to run in live mode")
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", default="")
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="kairos-fixture-"))
    s = Settings(database_url=f"sqlite:///{tmp}/k.db", data_dir=str(tmp), public_origin=f"http://127.0.0.1:{args.port}")
    store = Store(make_engine(s.database_url))
    vault = Vault(b"f" * 32)
    uid = store.create_user("demo", hash_password("fixture-password"), "x", "owner", Decimal("100"))
    from sqlalchemy import update

    from kairos.db import schema as S
    with store.engine.begin() as c:
        c.execute(update(S.users).where(S.users.c.id == uid).values(totp_secret_enc=vault.encrypt(TOTP_SECRET.encode(), aad=f"totp:{uid}".encode())))
    market = MarketService(s, store, None, None)  # rest attached below
    rest = MovingRest(time.time(), market)
    rest.ticker = "KXBTC15M-FIXTURE"
    orig_m = rest._m

    def _m(status="active"):
        d = orig_m(status)
        d["floor_strike"] = "97780.00"
        d["title"] = "BTC price up in next 15 mins? (fixture)"
        return d
    rest._m = _m
    market.rest = rest
    market.verified_templates.add(parse_rule(parse_market(rest._m())).template)
    eng = Engine(s, store, market)
    eng.fixture_label = "LAYOUT TEST — SYNTHETIC FIXTURE DATA, NOT MARKET DATA"
    eng.load_accounts()
    app = create_app(eng, vault, dev=True)

    @app.on_event("startup")
    async def _start():
        await eng.run(use_external=False)
        asyncio.create_task(synth(market))

    if args.static:
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=args.static, html=True), name="static")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
