from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from kairos.config import ConfigError, Settings, load_settings, validate
from kairos.feeds.base import Quote
from kairos.feeds.composite import Composite
from kairos.feeds import coinbase, kraken
from kairos.kalshi.auth import AuthError, KalshiSigner, signing_path, verify
from kairos.kalshi.parse import BookDelta, ParseError, parse_delta, parse_market, parse_orderbook, parse_trade
from kairos.market.book import OrderBook
from kairos.market.rules import (FinalMinuteSampler, RulesError, WindowClock, parse_rule, required_remaining_average,
                                 rules_template)
from kairos.money import FeeAccumulator, FeeSchedule, MoneyError, ceil_cent

FEES = FeeSchedule("quadratic")


# ---------------------------------------------------------------- fees (p.14)
def test_worked_scalp_fees():
    assert FEES.fee(50, "0.40") == Decimal("0.84")
    assert FEES.fee(50, "0.60") == Decimal("0.84")
    outlay = 50 * Decimal("0.40") + FEES.fee(50, "0.40")
    proceeds = 50 * Decimal("0.60") - FEES.fee(50, "0.60")
    assert outlay == Decimal("20.84") and proceeds == Decimal("29.16") and proceeds - outlay == Decimal("8.32")


def test_strict_bankroll_example():
    assert FEES.fee(47, "0.40") == Decimal("0.79")
    assert 47 * Decimal("0.40") + FEES.fee(47, "0.40") == Decimal("19.59")
    assert 47 * Decimal("0.60") - FEES.fee(47, "0.60") - (47 * Decimal("0.40") + FEES.fee(47, "0.40")) == Decimal("7.82")


def test_maker_fee_and_types():
    assert FEES.fee(100, "0.50", maker=True) == 0
    mk = FeeSchedule("quadratic_with_maker_fees")
    assert mk.fee(100, "0.50", maker=True) == Decimal("0.44")
    assert mk.fee(100, "0.50") == Decimal("1.75")


def test_unknown_schedule_disables():
    with pytest.raises(MoneyError):
        FeeSchedule("unknown").fee(1, "0.5")
    assert FeeSchedule.from_series({}).known is False
    assert FeeSchedule.from_series({"fee_type": "quadratic", "fee_multiplier": 0.5}).fee(100, "0.5") == Decimal("0.88")


def test_fee_accumulator_rounds_per_order():
    acc = FeeAccumulator(FEES)
    # 10 one-contract fills at 50c: per-fill rounding would give 10 * 0.02 = 0.20; accumulator gives 0.18
    total = sum(acc.add_fill(1, "0.50") for _ in range(10))
    assert total == ceil_cent(Decimal("0.07") * 10 * Decimal("0.25")) == Decimal("0.18")


def test_fee_precision_bounds():
    with pytest.raises(MoneyError):
        FEES.fee(1, "1.5")
    assert FEES.fee(0, "0.5") == 0


# ---------------------------------------------------------------- config (p.50, p.100)
def test_config_defaults_safe():
    s = load_settings(env={})
    assert s.is_paper and not s.research.research_can_trade and not s.social_sources_enabled
    assert not s.research.claude_enabled and not s.risk.aggressive_enabled


@pytest.mark.parametrize("env", [{"KAIROS_MODE": "yolo"}])
def test_config_rejects_bad_mode(env):
    with pytest.raises(ConfigError):
        load_settings(env=env)


def test_config_rejects_unsafe_overrides(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"research": {"research_can_trade": true}}')
    with pytest.raises(ConfigError):
        load_settings(p, env={})
    p.write_text('{"social_sources_enabled": true}')
    with pytest.raises(ConfigError):
        load_settings(p, env={})
    p.write_text('{"nonsense": 1}')
    with pytest.raises(ConfigError):
        load_settings(p, env={})
    p.write_text('{"risk": {"allowed_series": ["KXETH15M"]}}')
    with pytest.raises(ConfigError):
        load_settings(p, env={})
    p.write_text('{"risk": {"max_entry_fraction": "0.5"}}')
    with pytest.raises(ConfigError):
        load_settings(p, env={})


def test_policy_frozen():
    s = Settings()
    with pytest.raises(Exception):
        s.risk.daily_loss_fraction = Decimal("0.9")  # type: ignore[misc]


# ---------------------------------------------------------------- auth (p.4)
@pytest.fixture(scope="module")
def signer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return KalshiSigner.from_pem("test-key-id", pem)


def test_signing_path_strips_query():
    assert signing_path("https://x/trade-api/v2/portfolio/orders?limit=5") == "/trade-api/v2/portfolio/orders"
    assert signing_path("/trade-api/v2/markets?x=1") == "/trade-api/v2/markets"
    with pytest.raises(AuthError):
        signing_path("markets")


def test_sign_verify(signer):
    h = signer.headers("get", "/trade-api/v2/portfolio/balance?x=1", now_ms=1700000000000)
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000" and h["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert verify(signer.public_key(), h["KALSHI-ACCESS-SIGNATURE"], 1700000000000, "GET", "/trade-api/v2/portfolio/balance")
    assert not verify(signer.public_key(), h["KALSHI-ACCESS-SIGNATURE"], 1700000000001, "GET", "/trade-api/v2/portfolio/balance")
    assert repr(signer) == "KalshiSigner(key_id=test…)"  # key id truncated, no key material


def test_key_permission_enforced(tmp_path, signer):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "k.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    p.chmod(0o644)
    with pytest.raises(AuthError):
        KalshiSigner.from_file("id", p)
    p.chmod(0o600)
    assert KalshiSigner.from_file("id", p).key_id == "id"


def test_invalid_key_rejected():
    with pytest.raises(Exception):
        KalshiSigner.from_pem("id", b"not a key")


# ---------------------------------------------------------------- parsing + book (p.6, p.13, p.133)
MARKET = {
    "ticker": "KXBTC15M-26NOV241215", "event_ticker": "KXBTC15M-26NOV241215", "status": "active",
    "open_time": "2026-11-24T17:00:00Z", "close_time": "2026-11-24T17:15:00Z",
    "yes_bid_dollars": "0.5200", "yes_ask_dollars": "0.5400", "no_bid_dollars": "0.4600", "no_ask_dollars": "0.4800",
    "floor_strike": 97780.12, "volume_fp": "1200.00",
    "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) before 12:15 PM EST is at least the same average before 12:00 PM EST, then the market resolves to Yes.",
}


def test_parse_market_dollars():
    m = parse_market(MARKET)
    assert m.series_ticker == "KXBTC15M" and m.yes_bid == Decimal("0.52") and m.strike == Decimal("97780.12")
    assert m.raw_variant == "dollars" and not m.missing_critical and m.is_open


def test_parse_market_legacy_cents_and_missing():
    m = parse_market({"ticker": "KXBTC15M-X", "yes_bid": 52, "status": "open"})
    assert m.yes_bid == Decimal("0.52") and m.yes_ask is None  # missing stays missing, never zero
    assert "close_time" in m.missing_critical


def test_parse_orderbook_variants():
    fp = parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.52", "100.00"], ["0.51", "4.04"]], "no_dollars": [["0.46", "50"]]}})
    legacy = parse_orderbook({"orderbook": {"yes": [[52, 100]], "no": [[46, 50]]}})
    assert fp.yes_bids[0] == (Decimal("0.52"), Decimal("100.00")) and fp.yes_bids[1][1] == Decimal("4.04")
    assert legacy.no_bids == [(Decimal("0.46"), Decimal("50"))]
    with pytest.raises(ParseError):
        parse_orderbook({"foo": 1})


def _book():
    b = OrderBook("T")
    b.apply_snapshot(parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.50", "10"], ["0.52", "5"]], "no_dollars": [["0.46", "8"], ["0.45", "20"]]}}), seq=1, receive_ts=0)
    return b


def test_complement():
    b = _book()
    assert b.best_yes_bid() == Decimal("0.52") and b.best_yes_ask() == Decimal("0.54")
    assert b.best_no_bid() == Decimal("0.46") and b.best_no_ask() == Decimal("0.48")
    assert b.spread_yes() == Decimal("0.02")


def test_walk_depth_no_overfill():
    b = _book()
    f = b.buy("yes", Decimal("12"))
    assert f.quantity == 12 and f.cost == 8 * Decimal("0.54") + 4 * Decimal("0.55")
    f2 = b.buy("yes", Decimal("100"))
    assert f2.quantity == 28  # never exceeds visible liquidity
    s = b.sell("yes", Decimal("7"), limit=Decimal("0.51"))
    assert s.quantity == 5  # limit stops at 0.52 level only


def test_seq_gap_invalidates():
    b = _book()
    assert b.apply_delta(BookDelta("yes", Decimal("0.52"), Decimal("-5")), seq=2, receive_ts=1)
    assert b.best_yes_bid() == Decimal("0.50")
    assert not b.apply_delta(BookDelta("yes", Decimal("0.53"), Decimal("1")), seq=4, receive_ts=2)
    assert not b.valid and b.best_yes_ask() is None and b.stale_reason.startswith("seq_gap")


def test_crossed_book_rejected():
    b = OrderBook("T")
    b.apply_snapshot(parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.60", "1"]], "no_dollars": [["0.50", "1"]]}}), 1, 0)
    assert not b.valid and b.stale_reason == "crossed_book"


def test_parse_delta_and_trade():
    d = parse_delta({"side": "no", "price_dollars": "0.4500", "delta_fp": "-3.00"})
    assert d.delta == Decimal("-3")
    t = parse_trade({"trade_id": "1", "ticker": "T", "created_time": "2026-11-24T17:01:00Z", "no_price_dollars": "0.40", "count_fp": "2"})
    assert t.yes_price == Decimal("0.60")


def test_microprice():
    b = _book()
    # bid 0.52 x 5, ask 0.54 x 8 → (0.54*5 + 0.52*8)/13
    assert b.microprice_yes() == (Decimal("0.54") * 5 + Decimal("0.52") * 8) / 13


# ---------------------------------------------------------------- rules (p.2, p.27)
def test_rules_parsing_and_template():
    m = parse_market(MARKET)
    r = parse_rule(m)
    assert r.comparator == ">=" and r.averaging_seconds == 60 and r.source == "CF Benchmarks"
    m2 = parse_market({**MARKET, "rules_primary": MARKET["rules_primary"].replace("12:15", "12:30").replace("12:00", "12:15")})
    assert parse_rule(m2).template == r.template  # same template across windows
    assert r.yes_wins(Decimal("100"), Decimal("100"))  # tie → YES under "at least"
    with pytest.raises(RulesError):
        parse_rule(parse_market({**MARKET, "rules_primary": "Something unrelated."}))


def test_required_average():
    # 30 of 60 observed summing to 30*100 with strike 101 → remaining must average 102
    assert required_remaining_average(Decimal(101), Decimal(3000), 30) == Decimal(102)
    assert required_remaining_average(Decimal(101), Decimal(6000), 60) is None
    with pytest.raises(RulesError):
        required_remaining_average(Decimal(1), Decimal(0), 61)


def test_sampler_missing_not_zero():
    s = FinalMinuteSampler(window_start=1000.0)
    s.observe(1000.2, Decimal(10))
    s.observe(1002.5, Decimal(12))
    s.observe(1002.9, Decimal(99))  # duplicate second ignored
    assert s.n == 2 and s.total == 22 and s.missing(1004) == 2


def test_clock():
    c = WindowClock.from_meta(parse_market(MARKET))
    t0 = c.open_ts
    assert c.phase(t0 - 1) == "pre_open" and c.phase(t0 + 100) == "open"
    assert c.phase(c.close_ts - 30) == "final_minute" and c.phase(c.close_ts) == "closed"
    assert c.bucket(t0 + 200) == "3-7"


# ---------------------------------------------------------------- feeds (p.3)
def test_composite_median_and_staleness():
    c = Composite(max_age_ms=1000)
    c.update(Quote("coinbase", "BTC", 100, 102, None, 10.0))
    c.update(Quote("kraken", "BTC", 104, 106, None, 10.0))
    p = c.price("BTC", 10.5)
    assert p.value == 103 and p.label == "exchange_proxy" and p.venues == ("coinbase", "kraken")
    assert c.price("BTC", 12.0) is None  # stale → unavailable, not last value


def test_coinbase_kraken_normalize():
    qs, ts, seq = coinbase.normalize({"channel": "ticker", "sequence_num": 5, "timestamp": "2026-11-24T00:00:00Z",
                                      "events": [{"tickers": [{"product_id": "BTC-USD", "best_bid": "1", "best_ask": "2"}]}]}, 1.0)
    assert qs[0].mid == 1.5 and seq == 5
    _, trs, _ = coinbase.normalize({"channel": "market_trades", "events": [{"trades": [{"product_id": "ETH-USD", "price": "3", "size": "1", "side": "BUY"}]}]}, 1.0)
    assert trs[0].side is None  # aggressor unknown until verified
    kq, kt = kraken.normalize({"channel": "trade", "type": "update", "data": [{"symbol": "SOL/USD", "price": 1, "qty": 2, "side": "sell"}]}, 1.0)
    assert kt[0].side == "sell" and kt[0].asset == "SOL"
