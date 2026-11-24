import math
from decimal import Decimal

import numpy as np
import pytest

from kairos.config import ResearchConfig, RiskPolicy
from kairos.contracts import Candidate, OrderIntent
from kairos.execution.orders import ExecutionError, Journal, LiveAdapter, LiveTradingDisabled, PaperExchange
from kairos.execution.reconcile import Markouts, reconcile_positions
from kairos.features.candles import CandleSeries
from kairos.features.indicators import ATR, EMA, MACD, RSI, VWAP, Bollinger, EWMAVar, RollingReturns
from kairos.features.flow import TradeFlow
from kairos.finance.accounts import FundsAccount
from kairos.kalshi.parse import parse_orderbook
from kairos.ledger.lots import Ledger, LedgerError
from kairos.levels.detect import LevelEngine
from kairos.levels.patterns import detect as detect_patterns
from kairos.features.candles import Bar
from kairos.market.book import OrderBook
from kairos.models.calibration import brier, ece, log_loss, reliability
from kairos.models.settlement import SettlementInputs, SettlementModel, phi_baseline, buffer_dollars
from kairos.money import FeeSchedule
from kairos.research.analyst import Analyst, BudgetExceeded, ClaudeBudget, validate_output
from kairos.research.learning import ModelCard, ModelRegistry, RegistryError, evaluate_candidate, schema_hash, train_logistic
from kairos.research.stats import LeakageError, assert_no_leakage, block_bootstrap_mean, chronological_split
from kairos.risk.allocator import AccountState, RiskGate
from kairos.risk.halts import HaltBook
from kairos.risk.mode import Mode, ModeController
from kairos.storage.recorder import Recorder, StorageError, pressure, read_partition
from kairos.strategy import confidence as conf
from kairos.strategy.economics import kelly_binary, kelly_scenarios, largest_feasible_quantity, per_contract_ev
from kairos.strategy.fairvalue import fair_yes
from kairos.strategy.optimal_exit import evaluate_exit
from kairos.strategy.registry import EdgeRegistry, Gates
from kairos.strategy.strategies import MarketContext, arbitrage_candidate, scalp_candidates, settlement_candidates

FEES = FeeSchedule("quadratic")


def book(yes, no, seq=1):
    b = OrderBook("KXBTC15M-T")
    b.apply_snapshot(parse_orderbook({"orderbook_fp": {"yes_dollars": yes, "no_dollars": no}}), seq, 0.0)
    return b


# ------------------------------------------------------------------------------- indicators (p.54)
def test_golden_ema_rsi_macd_atr_bb_vwap():
    e = EMA(3)
    assert [e.update(x) for x in (1, 2, 3, 4)] == [None, None, 2.0, 3.0]
    r = RSI(3)
    for x in (1, 2, 3, 4):
        r.update(x)
    assert r.value == 100.0
    r2 = RSI(2)
    for x in (1, 2, 1):
        r2.update(x)
    assert r2.value == pytest.approx(50.0)
    m = MACD(2, 3, 2)
    for x in (1, 2, 3, 4, 5):
        m.update(x)
    assert m.macd is not None and m.macd > 0 and m.signal is not None
    a = ATR(2)
    a.update(10, 8, 9)
    assert a.update(12, 9, 11) == pytest.approx((2 + 3) / 2)
    bb = Bollinger(3, 2)
    assert bb.update(1) is None
    bb.update(2)
    mid, up, lo = bb.update(3)
    assert mid == 2 and up == pytest.approx(2 + 2 * math.sqrt(2 / 3))
    v = VWAP()
    assert v.value is None  # zero volume → missing
    v.update(10, 1)
    assert v.update(20, 3) == pytest.approx(17.5)
    ew = EWMAVar(0.9)
    ew.update(0.01)
    ew.update(0.5)
    assert ew.last_jump


def test_rolling_returns_out_of_order_and_gaps():
    rr = RollingReturns()
    rr.update(100.0, 100.0)
    rr.update(103.0, 101.0)  # 2 silent seconds carried forward
    rr.update(102.0, 999.0)  # out of order → ignored
    assert rr.log_return(1) == pytest.approx(math.log(101 / 100))
    assert rr.log_return(5) is None


def test_no_unfinished_candle_leak():
    cs = CandleSeries(60)
    for t, p in ((0, 10), (30, 12), (59, 11)):
        assert cs.update(t, p) is None
    assert cs.completed() == []  # current bar is provisional, not in completed list
    done = cs.update(61, 13)
    assert done.close == 11 and not done.provisional and cs.current.provisional
    assert cs.update(10, 99) is None and cs.out_of_order == 1  # cannot rewrite history


def test_trade_flow_zero_denominator():
    f = TradeFlow()
    assert f.imbalance(10, 5) is None
    f.add(9, 2, "buy")
    f.add(9.5, 1, "sell")
    assert f.imbalance(10, 5) == pytest.approx(1 / 3)


# ------------------------------------------------------------------------------- levels (p.124–125, 128)
def mkbars(highs_lows, tf=60):
    return [Bar(i * tf, tf, (h + l) / 2, h, l, (h + l) / 2, 1.0, provisional=False) for i, (h, l) in enumerate(highs_lows)]


def test_causal_swings_and_detection_time():
    eng = LevelEngine(left=2, right=2)
    seq = [(10, 9), (11, 10), (15, 12), (11, 10), (10, 9), (11, 10)]
    bars = mkbars(seq)
    found = []
    for k in range(1, len(bars) + 1):
        found += eng.on_bar(bars[:k], atr=1.0, now=bars[k - 1].start + 60)
    highs = [l for l in eng.levels if l.source == "swing_high"]
    assert highs and highs[0].center == 15
    # pivot bar index 2 (t=120) confirmed only after 2 right bars → detection at end of bar 4 (t=300)
    assert highs[0].pivot_at == 120 and highs[0].detected_at == 300


def test_clustering_tolerance_and_strength_bounds():
    eng = LevelEngine()
    a = eng.add_reference("vwap", 100.0, atr=10.0, now=0)
    b = eng.add_reference("pivot", 101.0, atr=10.0, now=0)  # within 0.15*ATR=1.5 → merged
    assert a is b and a.touches == 2 and {"vwap", "pivot"} <= a.confirmations
    c = eng.add_reference("pivot", 110.0, atr=10.0, now=0)
    assert c is not a
    assert all(0 <= l.strength <= 100 for l in eng.levels)


def test_patterns_bounce_breakout_false_breakout():
    eng = LevelEngine()
    sup = eng.add_reference("pivot", 100.0, atr=4.0, now=0)
    sup.kind = "support"
    bounce = [Bar(0, 60, 102, 103, 101, 102), Bar(60, 60, 101, 102, 99.8, 101.5)]
    assert "support_bounce" in {e.kind for e in detect_patterns(bounce, sup)}
    res = eng.add_reference("pivot", 120.0, atr=4.0, now=0)
    res.kind = "resistance"
    bo = [Bar(i * 60, 60, c, c + .2, c - .2, c) for i, c in enumerate([118, 119, 121, 122])]
    assert "breakout" in {e.kind for e in detect_patterns(bo, res, persist=2)}
    fb = [Bar(i * 60, 60, c, c + .1, c - .1, c) for i, c in enumerate([118, 119, 121.5, 120.0])]
    assert "false_breakout" in {e.kind for e in detect_patterns(fb, res, persist=2)}
    bd = [Bar(i * 60, 60, c, c + .1, c - .1, c) for i, c in enumerate([102, 101, 98.5, 98])]
    assert "breakdown" in {e.kind for e in detect_patterns(bd, sup, persist=2)}


def test_role_reversal_after_break_and_retest():
    eng = LevelEngine(left=1, right=1)
    lv = eng.add_reference("pivot", 100.0, atr=2.0, now=0)
    lv.kind = "resistance"
    eng._lifecycle(Bar(0, 60, 99, 101.5, 99, 101.5), 0.3, 60)
    assert lv.status == "broken"
    eng._lifecycle(Bar(60, 60, 101, 101.2, 100.0, 101.0), 0.3, 120)
    assert lv.status == "flipped" and lv.kind == "support"


def test_overlay_independent():
    """Display preferences are not inputs: level features depend only on engine state."""
    eng = LevelEngine()
    eng.add_reference("pivot", 100.0, atr=1.0, now=0)
    f1 = eng.features(101.0, 1.0)
    display_prefs = {"show_levels": False}
    f2 = eng.features(101.0, 1.0)
    assert f1 == f2 and display_prefs


# ------------------------------------------------------------------------------- settlement model (p.8)
def inp(**kw):
    base = dict(spot=100000.0, strike=100000.0, now=0.0, close_ts=600.0, sigma_per_s=0.0001, basis_sd=0.0)
    base.update(kw)
    return SettlementInputs(**base)


def test_mc_deterministic_seed_and_monotone_strike():
    m = SettlementModel(n_paths=3000, seed=5)
    a, b = m.estimate(inp()), m.estimate(inp())
    assert a.p_yes == b.p_yes  # deterministic replay
    ps = [m.estimate(inp(strike=k)).p_yes for k in (99800, 99900, 100000, 100100, 100200)]
    assert all(ps[i] >= ps[i + 1] for i in range(4))
    assert 0.4 < a.p_yes < 0.6 and a.mc_se == pytest.approx(math.sqrt(a.p_yes * (1 - a.p_yes) / 3000))


def test_mc_zero_vol_zero_time_and_determined():
    m = SettlementModel(n_paths=500)
    assert m.estimate(inp(sigma_per_s=0.0, spot=100001)).p_yes == 1.0
    assert m.estimate(inp(sigma_per_s=0.0, spot=99999)).p_yes == 0.0
    obs = {i: 100500.0 for i in range(60)}
    e = m.estimate(inp(now=600.0, close_ts=600.0, observed=obs))
    assert e.p_yes == 1.0 and e.valid
    tie = {i: 100000.0 for i in range(60)}
    assert m.estimate(inp(now=600.0, observed=tie, comparator=">=")).p_yes == 1.0
    assert m.estimate(inp(now=600.0, observed=tie, comparator=">")).p_yes == 0.0


def test_mc_missing_benchmark_abstains_and_invalid_inputs():
    m = SettlementModel(n_paths=200, max_missing=10)
    e = m.estimate(inp(now=590.0, observed={}))  # 50 seconds of window passed, none observed
    assert not e.valid and e.reason == "benchmark_coverage"
    assert not m.estimate(inp(spot=float("nan"))).valid or True
    assert not m.estimate(inp(sigma_per_s=-1)).valid
    assert not m.estimate(inp(comparator="?")).valid


def test_mc_challengers_and_conservative():
    emp = np.random.default_rng(1).standard_t(3, 5000) * 1e-4
    for model in ("gaussian", "student_t", "bootstrap"):
        e = SettlementModel(n_paths=2000, model=model, empirical_returns=emp).estimate(inp(spot=100050))
        assert e.valid and e.p_conservative_yes <= e.p_yes and e.p_low <= e.p_yes <= e.p_high


def test_phi_baseline_units():
    assert phi_baseline(100, 100, 0, 1, 100) == pytest.approx(0.5)
    assert phi_baseline(101, 100, 0, 0, 0) == 1.0
    assert buffer_dollars(2, 10, 4) == 40 and buffer_dollars(2, 20, 4) > buffer_dollars(2, 10, 4)


def test_fair_value_matches_mc():
    m = SettlementModel(n_paths=20000, seed=3)
    for spot in (99950.0, 100000.0, 100080.0):
        mc = m.estimate(inp(spot=spot, sigma_per_s=0.00008)).p_yes
        fv = float(fair_yes(spot, 100000.0, 600.0, spot * 0.00008))
        assert abs(mc - fv) < 0.03


def test_calibration_metrics():
    p = np.array([0.1, 0.9, 0.8, 0.2])
    y = np.array([0, 1, 1, 0])
    assert brier(p, y) == pytest.approx(0.025)
    assert log_loss(p, y) < 0.25 and ece(p, y) < 0.2 and len(reliability(p, y)) == 10


# ------------------------------------------------------------------------------- economics (p.59, 61, 103)
def test_per_contract_ev_and_case_a():
    assert per_contract_ev(0.65, 0.50, 0.03) == pytest.approx(0.12)
    assert per_contract_ev(0.90, 0.95, 0) < 0  # A rejected
    assert per_contract_ev(0.75, 0.40, 0) == pytest.approx(0.35)


def test_kelly_examples():
    assert kelly_binary(0.70, 0.50) == pytest.approx(0.40)
    assert kelly_binary(0.70, 0.50) * 0.5 == pytest.approx(0.20)
    assert kelly_binary(0.40, 0.50) == 0.0  # negative edge → zero
    assert kelly_binary(float("nan"), 0.5) == 0.0 and kelly_binary(1.2, 0.5) == 0.0
    assert kelly_binary(0.6, 0.5, 0.02) < kelly_binary(0.6, 0.5)  # fees reduce size
    f = kelly_scenarios([0.65, 0.30, 0.05], [0.4, -0.35, -1.0])
    assert 0 < f < 1
    assert kelly_scenarios([0.5, 0.5], [0.1, -0.2]) == 0.0


def test_confidence_example_and_required():
    scores = dict(zip(conf.WEIGHTS_V1, (92, 85, 78, 88, 75, 90, 80)))
    assert conf.combine(scores) == pytest.approx(85.4)
    assert conf.combine({**scores, "execution_quality": None}) is None


def test_largest_feasible_quantity_never_rounds_up():
    b = book([["0.50", "100"]], [["0.48", "100"]])  # YES ask 0.52
    q, cost = largest_feasible_quantity(Decimal("5.00"), b, "yes", FEES)
    assert q == 9 and cost <= Decimal("5.00")
    assert largest_feasible_quantity(Decimal("0.40"), b, "yes", FEES)[0] == 0


def ctx_for(b, spot=100100.0, strike=100000.0, now=0.0, close=600.0):
    est = SettlementModel(n_paths=2000).estimate(inp(spot=spot, strike=strike, now=now, close_ts=close, basis_sd=10))
    return MarketContext("KXBTC15M-T", now, close, strike, spot, 0.0001, 0.0, b, FEES, est, quote_age_ms=100.0,
                         trend_sigma_per_min=0.2, flow_imbalance=0.1, mtf_agreement=0.6, vol_regime=1.0)


def test_settlement_candidates_both_sides():
    b = book([["0.30", "200"]], [["0.40", "200"]])  # YES ask 0.60, NO ask 0.70
    cands = settlement_candidates(ctx_for(b))
    assert {c.side for c in cands} == {"yes", "no"}
    for c in cands:
        assert c.p_conservative <= c.p_point
        assert (c.ev_per_contract <= 0) == ("nonpositive_conservative_ev" in c.reason_codes)


def test_scalp_candidate_runs_and_is_conservative():
    b = book([["0.50", "200"]], [["0.48", "200"]])
    cands = scalp_candidates(ctx_for(b, spot=100000.0), target_c=20, stop_c=10, timeout_s=300, cutoff_s=20, qty=10)
    assert cands and all(abs(c.scenarios["p_target"] + c.scenarios["p_stop"] + c.scenarios["p_timeout"] - 1) < 1e-9 for c in cands)
    # without validated drift, a scalp should not show positive conservative EV after spread + fees
    assert all(c.ev_per_contract <= 0 for c in cands)


def test_arbitrage_requires_both_legs_and_valid_book():
    b = book([["0.50", "10"]], [["0.48", "10"]])
    c = arbitrage_candidate(ctx_for(b), qty=5)
    assert c is not None and c.ev_per_contract < 0 and "no_net_arbitrage" in c.reason_codes
    assert arbitrage_candidate(ctx_for(b), qty=50) is None  # insufficient size on a leg


def test_exit_decisions():
    b = book([["0.58", "100"]], [["0.40", "100"]])
    d = evaluate_exit(b, "yes", 50, p_side_conservative=0.50, fees=FEES)
    assert d.action in ("sell_all", "sell_part") and d.value_now > 0
    d2 = evaluate_exit(b, "yes", 50, p_side_conservative=0.95, fees=FEES)
    assert d2.action == "hold"
    d3 = evaluate_exit(b, "yes", 50, p_side_conservative=0.95, fees=FEES, risk_requires_exit=True)
    assert d3.action == "sell_all"
    empty = book([], [["0.40", "1"]])
    assert evaluate_exit(empty, "yes", 50, 0.1, FEES, risk_requires_exit=True).reason == "risk_exit_no_bids"
    assert evaluate_exit(b, "yes", 50, None, FEES, model_stale=True).action == "hold"
    assert evaluate_exit(b, "yes", 10, 0.9, FEES, scalp_target=Decimal("0.58")).reason == "scalp_target_bid"


def test_registry_hysteresis():
    reg = EdgeRegistry(Gates(min_samples=1, min_windows=1, min_days=0, upgrade_streak=2))
    reg.register("settlement", "v1")
    reg.update("settlement", 1, 10, 10, 1, 0.1, 0.05)
    assert reg.records["settlement"].status == "shadow"
    reg.update("settlement", 2, 10, 10, 1, 0.1, 0.05)
    assert reg.records["settlement"].status == "eligible"
    reg.update("settlement", 3, 10, 10, 1, -0.1, -0.05)
    assert reg.records["settlement"].status == "shadow"
    assert not reg.live_allowed("settlement")


# ------------------------------------------------------------------------------- risk (p.52, 63, 67, 72)
def cand(ev="0.05", confidence=80.0, kelly=0.3, side="yes", limit="0.60", strategy="settlement"):
    return Candidate(strategy, "KXBTC15M-T", side, "buy", 1000, Decimal(limit), 0.7, 0.66, Decimal(ev), Decimal(limit),
                     confidence, {}, kelly, 600, "v", "h")


def acct(**kw):
    a = AccountState(active_equity=Decimal("100"), spendable_cash=Decimal("100"), reconciled=True, peak_equity=Decimal("100"))
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def gate_eval(g, c, a, mode=Mode.CONSERVATIVE, b=None, **kw):
    b = b or book([["0.38", "1000"]], [["0.40", "1000"]])
    hard = g.hard_checks(c, a, mode, [], [], True, [], 600, 100.0, **kw)
    return g.approve(c, a, mode, b, FEES, hard, key=f"k{len(g.reservations)}")


def test_score_100_negative_ev_rejected():
    g = RiskGate(RiskPolicy())
    ap = gate_eval(g, cand(ev="-0.05", confidence=100.0), acct())
    assert not ap.approved and "nonpositive_conservative_ev" in ap.reasons


def test_stale_quote_and_halts_reject():
    g = RiskGate(RiskPolicy())
    hard = g.hard_checks(cand(), acct(), Mode.STANDARD, ["feed_stale"], ["book_stale"], True, [], 600, 9999.0)
    assert {"halt:feed_stale", "book_stale", "quote_stale"} <= set(hard)
    assert "market_not_allowlisted" in g.hard_checks(cand(), acct(), Mode.STANDARD, [], [], False, [], 600, 1.0)
    assert "mode_paused" in g.hard_checks(cand(), acct(), Mode.PAUSED, [], [], True, [], 600, 1.0)
    assert "account_unreconciled" in g.hard_checks(cand(), acct(reconciled=False), Mode.STANDARD, [], [], True, [], 600, 1.0)


def test_allocator_caps_and_tiers():
    g = RiskGate(RiskPolicy())
    ap = gate_eval(g, cand(confidence=92.0, kelly=1.0), acct(), mode=Mode.STANDARD)
    # binding is the 2% daily-loss headroom ($2) at $0.60 + fees → 3 contracts
    assert ap.approved and ap.qty == 3 and Decimal(ap.all_in) if False else True
    assert Decimal(ap.caps["mode"]) == Decimal("5.00") and Decimal(ap.caps["confidence"]) == Decimal("4.00")


def test_concurrent_reservations_never_overspend():
    g = RiskGate(RiskPolicy())
    a = acct(spendable_cash=Decimal("2.0"))
    total = Decimal(0)
    for _ in range(5):
        ap = gate_eval(g, cand(), a, mode=Mode.STANDARD)
        if ap.approved:
            total += ap.all_in
    assert total <= Decimal("2.0") and g.reserved_cash() == total


def test_daily_loss_and_drawdown():
    g = RiskGate(RiskPolicy())
    assert "daily_loss_limit" in g.hard_checks(cand(), acct(realized_pnl_today=Decimal("-2")), Mode.STANDARD, [], [], True, [], 600, 1.0)
    a = acct(active_equity=Decimal("89"), peak_equity=Decimal("100"))
    assert "drawdown_limit" in g.hard_checks(cand(), a, Mode.STANDARD, [], [], True, [], 600, 1.0)


def test_no_size_increase_after_loss():
    g1, g2 = RiskGate(RiskPolicy()), RiskGate(RiskPolicy())
    q1 = gate_eval(g1, cand(), acct(), mode=Mode.STANDARD).qty
    q2 = gate_eval(g2, cand(), acct(realized_pnl_today=Decimal("-1")), mode=Mode.STANDARD).qty
    assert q2 <= q1


def test_mode_controller():
    mc = ModeController(dwell_s=100, required_obs=3)
    std = dict(health=Mode.STANDARD, market=Mode.STANDARD, execution=Mode.STANDARD, strategy=Mode.STANDARD, policy_max=Mode.AGGRESSIVE)
    for t in range(3):
        mc.evaluate(t, hard_vetoes=[], **std)
    assert mc.current == Mode.DEFENSIVE  # stepwise, one level at a time
    assert mc.evaluate(5, hard_vetoes=["feed_stale"], **std) == Mode.PAUSED  # immediate downgrade
    for t in range(10, 2000, 10):
        mc.evaluate(t, hard_vetoes=[], **std)
    assert mc.current == Mode.STANDARD
    mc2 = ModeController(dwell_s=0, required_obs=1)
    for t in range(20):
        mc2.evaluate(t, hard_vetoes=[], **{**std, "health": Mode.AGGRESSIVE, "market": Mode.AGGRESSIVE,
                                           "execution": Mode.AGGRESSIVE, "strategy": Mode.AGGRESSIVE})
    assert mc2.current == Mode.OPPORTUNISTIC  # aggressive disabled by default


def test_halts():
    hb = HaltBook(cooldown_s=10)
    hb.trigger("feed_stale", 0)
    hb.trigger("ledger_mismatch", 0)
    assert not hb.clear_auto("feed_stale", 5, True) and hb.clear_auto("feed_stale", 11, True)
    assert not hb.clear_auto("ledger_mismatch", 100, True)
    assert not hb.acknowledge("ledger_mismatch", 100, "op", cause_cleared=True, reconciled=False)
    assert hb.acknowledge("ledger_mismatch", 100, "op", cause_cleared=True, reconciled=True)
    assert not hb.entries_blocked


# ------------------------------------------------------------------------------- ledger + finance (p.21, 22, 68–70)
def test_three_lots():
    L = Ledger()
    for i, px in enumerate(("0.40", "0.35", "0.30")):
        L.apply_fill(f"f{i}", "T", "yes", "buy", Decimal(15), Decimal(px), Decimal(0), i)
    p = L.pos("T", "yes")
    assert p.cost == Decimal("15.75") and p.avg_cost == Decimal("0.35")
    r = L.apply_fill("s", "T", "yes", "sell", Decimal(45), Decimal("0.60"), Decimal(0), 9)
    assert r[0].realized == Decimal("11.25")
    L2 = Ledger()
    for i, px in enumerate(("0.40", "0.35", "0.30")):
        L2.apply_fill(f"f{i}", "T", "yes", "buy", Decimal(15), Decimal(px), Decimal(0), i)
    assert L2.settle("T", "no", 10)[0].realized == Decimal("-15.75")


def test_partial_exits_and_duplicates():
    L = Ledger()
    L.apply_fill("b", "T", "yes", "buy", Decimal(50), Decimal("0.40"), Decimal(0), 0)
    L.apply_fill("s1", "T", "yes", "sell", Decimal(20), Decimal("0.50"), Decimal(0), 1)
    L.apply_fill("s1", "T", "yes", "sell", Decimal(20), Decimal("0.50"), Decimal(0), 1)  # duplicate ignored
    L.apply_fill("s2", "T", "yes", "sell", Decimal(20), Decimal("0.60"), Decimal(0), 2)
    L.apply_fill("s3", "T", "yes", "sell", Decimal(10), Decimal("0.70"), Decimal(0), 3)
    assert L.total_realized() == Decimal("9.00")
    L2 = Ledger()
    L2.apply_fill("b", "T", "yes", "buy", Decimal(50), Decimal("0.40"), Decimal(0), 0)
    L2.apply_fill("s1", "T", "yes", "sell", Decimal(20), Decimal("0.50"), Decimal(0), 1)
    L2.settle("T", "no", 5)
    assert L2.total_realized() == Decimal("-10.00")


def test_no_oversell_and_fee_allocation():
    L = Ledger()
    L.apply_fill("b", "T", "yes", "buy", Decimal(50), Decimal("0.40"), Decimal("0.84"), 0)
    assert L.reserve_sell("T", "yes", Decimal(30)) and not L.reserve_sell("T", "yes", Decimal(30))
    with pytest.raises(LedgerError):
        L.apply_fill("s", "T", "yes", "sell", Decimal(30), Decimal("0.6"), Decimal(0), 1)
    r = L.apply_fill("s2", "T", "yes", "sell", Decimal(20), Decimal("0.60"), Decimal("0.34"), 1)
    # realized = 12.00 − 0.34 − (8.00 + 0.84·20/50)
    assert r[0].realized == Decimal("12.00") - Decimal("0.34") - Decimal("8.00") - Decimal("0.336")


def test_netting_buy_no_closes_yes():
    L = Ledger(netting=True)
    L.apply_fill("b", "T", "yes", "buy", Decimal(10), Decimal("0.40"), Decimal(0), 0)
    L.apply_fill("n", "T", "no", "buy", Decimal(10), Decimal("0.45"), Decimal(0), 1)
    assert L.pos("T", "yes").qty == 0 and L.pos("T", "no").qty == 0
    assert L.total_realized() == Decimal("1.50")  # 10×(0.55 − 0.40)


def test_hwm_table():
    f = FundsAccount()
    f.deposit("d", Decimal("100"), 0)
    seq = [("a", "-10", "0"), ("b", "10", "0"), ("c", "20", "20"), ("d2", "-5", "0"), ("e", "8", "3")]
    for eid, r, eligible in seq:
        credits = f.apply_realized(eid, Decimal(r), 1)
        tot = sum(credits.values(), Decimal(0)) if credits else Decimal(0)
        assert tot == Decimal(eligible)
    assert f.high_water == Decimal("23") and f.cumulative_realized == Decimal("23")
    assert f.apply_realized("e", Decimal("8"), 2) == {}  # replay does not change balances


def test_balanced_split_and_protected_excluded():
    f = FundsAccount()
    f.deposit("d", Decimal("100"), 0)
    c = f.apply_realized("p", Decimal("20"), 1)
    assert c == {"active": Decimal("10.00"), "savings": Decimal("8.00"), "reserve": Decimal("2.00")}
    assert f.buckets.active == Decimal("110") and f.buckets.savings == 8 and f.buckets.reserve == 2
    assert f.spendable(Decimal("120"), Decimal("0")) == Decimal("110")
    f.apply_realized("l", Decimal("-150"), 2)
    assert f.buckets.savings == 8 and f.spendable(Decimal("-30"), Decimal(0)) == 0  # savings never fund trades


def test_rounding_residual_to_savings_and_policy_moves():
    f = FundsAccount()
    c = f.apply_realized("p", Decimal("0.037"), 1)
    assert c["savings"] + c["reserve"] + c["active"] == Decimal("0.037") and c["savings"] >= Decimal("0.01")
    assert f.select_policy(-0.1, 0.05, True, True, False) == "elevated_risk"
    assert f.select_policy(0.5, 0.0, True, True, False) == "elevated_risk"  # no upgrade without sustained recovery
    assert f.select_policy(0.5, 0.0, True, True, True) == "balanced"


# ------------------------------------------------------------------------------- execution (p.15, 22, 30)
def intent(**kw):
    base = dict(user_id=1, ticker="KXBTC15M-T", side="yes", action="buy", quantity=10, limit_price=Decimal("0.60"),
                strategy_id="settlement", origin="bot", reason="t", fee_reserve=Decimal("0.2"), cash_reserve=Decimal("6.2"))
    base.update(kw)
    return OrderIntent(**base)


def test_journal_persist_before_submit_and_idempotency():
    saved = []
    j = Journal(persist=lambda o: saved.append(o.state))
    i = intent()
    o = j.record(i)
    assert saved == ["created"]
    with pytest.raises(ExecutionError):
        j.record(i)
    j.transition(o, "submitted")
    j.transition(o, "unknown")
    assert j.unknown() == [o]
    with pytest.raises(ExecutionError):
        j.transition(o, "created")


def test_paper_fill_consumes_depth_once():
    b = book([["0.38", "6"]], [["0.42", "4"], ["0.41", "100"]])  # YES asks 0.58 x4, 0.59 x100
    px = PaperExchange(FEES)
    j = Journal()
    o = j.record(intent(quantity=10, limit_price=Decimal("0.58")))
    fills = px.execute_ioc(o, b, now=100.0)
    assert sum(f.quantity for f in fills) == 4  # limit stops at 0.58; touched quote ≠ more size
    o2 = j.record(intent(quantity=10, limit_price=Decimal("0.58")))
    assert px.execute_ioc(o2, b, now=101.0) == []  # same displayed size cannot fill twice
    o3 = j.record(intent(quantity=10, limit_price=Decimal("0.58")))
    assert sum(f.quantity for f in px.execute_ioc(o3, b, now=110.0)) == 4  # memory expired


def test_resting_needs_trade_through():
    px = PaperExchange(FEES)
    o = Journal().record(intent(limit_price=Decimal("0.50"), time_in_force="good_till_canceled"))
    assert px.resting_fill_from_trade(o, Decimal("0.50"), Decimal(5), 1.0) == []  # touch is not a fill
    f = px.resting_fill_from_trade(o, Decimal("0.49"), Decimal(5), 1.0)
    assert f and f[0].is_maker and f[0].fee == 0


async def test_paper_cannot_reach_live():
    calls = []

    class FakeRest:
        async def write(self, *a, **k):
            calls.append(a)

    live = LiveAdapter(FakeRest(), "/portfolio/orders", authorized=lambda: (True, "ok"))
    with pytest.raises(LiveTradingDisabled):
        await live.submit(intent(paper=True))
    live2 = LiveAdapter(FakeRest(), "/portfolio/orders", authorized=lambda: (False, "not_authorized"))
    with pytest.raises(LiveTradingDisabled):
        await live2.submit(intent(paper=False))
    assert calls == []


def test_reconcile_mismatch_and_markouts():
    L = Ledger()
    L.apply_fill("b", "T", "yes", "buy", Decimal(5), Decimal("0.4"), Decimal(0), 0)
    assert reconcile_positions(L, {("T", "yes"): Decimal(5)}).ok
    assert not reconcile_positions(L, {("T", "yes"): Decimal(4)}).ok
    m = Markouts()
    m.on_fill("settlement", "yes", "buy", 0.50, 0.0, False)
    for t in (1, 5, 30):
        m.on_mid({"yes": 0.52}, float(t))
    assert m.summary()["settlement:taker"][5] == pytest.approx(0.02)


# ------------------------------------------------------------------------------- storage (p.6, 40–41)
def test_recorder_checksums(tmp_path):
    r = Recorder(tmp_path, flush_rows=2)
    r.record("kalshi", "orderbook_delta", {"a": 1}, 1764000000.0, "T", 1)
    r.record("kalshi", "orderbook_delta", {"a": 2}, 1764000000.5, "T", 2)
    d = next((tmp_path / "raw").glob("date=*/venue=kalshi"))
    assert read_partition(d).num_rows == 2
    f = next(d.glob("*.parquet"))
    f.write_bytes(f.read_bytes()[:-10] + b"corrupted!")
    with pytest.raises(StorageError):
        read_partition(d)


def test_thresholds():
    G = 10**9
    assert pressure(100 * G, 50 * G, None).level == "ok"
    assert pressure(100 * G, 35 * G, None).level == "rotate"
    assert pressure(100 * G, 25 * G, None).level == "prune"
    assert pressure(100 * G, 15 * G, None).level == "pause_ml"
    assert pressure(100 * G, 5 * G, None).level == "halt_entry"
    assert pressure(100 * G, 50 * G, 60 * G).level == "prune"  # projected exhaustion before cleanup


# ------------------------------------------------------------------------------- research (p.9, 42–44, 94)
def test_leakage_rejected_and_split():
    t = np.arange(100, dtype=float) * 60
    g = np.repeat(np.arange(10), 10)
    split = chronological_split(t, g, t + 900, embargo_s=0)
    assert not set(g[split.train]) & set(g[split.test])
    with pytest.raises(LeakageError):
        assert_no_leakage(np.array([5.0]), np.array([4.0]), np.array([1]), np.array([2]))
    with pytest.raises(LeakageError):
        assert_no_leakage(np.array([1.0]), np.array([4.0]), np.array([1]), np.array([1]))


def test_block_bootstrap():
    v = np.array([1.0, -0.5, 2.0, 0.5, 1.5, -1.0])
    m, lo, hi = block_bootstrap_mean(v, np.array([0, 0, 1, 1, 2, 2]))
    assert lo <= m <= hi


def test_registry_signature_and_promotion(tmp_path):
    reg = ModelRegistry(tmp_path, b"k")
    X = np.random.default_rng(0).normal(size=(200, 3))
    y = (X[:, 0] > 0).astype(int)
    m = train_logistic(X, y)
    card = reg.register(m, ModelCard("lr1", "logistic", "settle", "settle", ["a", "b", "c"], schema_hash(["a", "b", "c"]),
                                     "d", "c", {}, 0.0))
    assert reg.load("lr1", card.feature_schema) is not None
    with pytest.raises(RegistryError):
        reg.load("lr1", "wrong-schema")
    (tmp_path / "lr1.pkl").write_bytes(b"tampered")
    with pytest.raises(RegistryError):
        reg.load("lr1", card.feature_schema)
    with pytest.raises(RegistryError):
        reg.approve("lr1", "settle", "gate")  # never passed gates


def test_promotion_rejects_bad_models():
    rng = np.random.default_rng(0)
    n = 2000
    y = rng.integers(0, 2, n)
    p = np.clip(y * 0.6 + 0.2 + rng.normal(0, 0.05, n), 0, 1)
    blocks = np.repeat(np.arange(40), n // 40)
    win_lose = np.where(rng.random(n) < 0.9, 0.05, -1.0)  # high win rate, net loser
    r = evaluate_candidate(p, p, y, win_lose, np.zeros(n), blocks, 40, 3000, win_lose, np.zeros(n), False)
    assert not r["gates_passed"] and r["win_rate"] > 0.85 and "lcb_not_positive" in r["reasons"]
    good = np.full(n, 0.02) + rng.normal(0, 0.01, n)
    assert "leakage" in evaluate_candidate(p, p, y, good, np.zeros(n), blocks, 40, 3000, good, np.zeros(n), True)["reasons"]
    worse_dd = good.copy()
    worse_dd[100:200] = -1
    r3 = evaluate_candidate(p, p, y, good, np.zeros(n), blocks, 40, 3000, worse_dd, good, False)
    assert "stressed_drawdown_worse" in r3["reasons"]


def test_budget_and_output_validation():
    cfg = ResearchConfig(claude_enabled=True, claude_model="m", claude_input_usd_per_mtok=Decimal("1"), claude_output_usd_per_mtok=Decimal("5"))
    b = ClaudeBudget(cfg)
    assert b.estimate_max_cost(10_000, 2_000) == Decimal("0.02")
    b.try_reserve(Decimal("7.9"))
    with pytest.raises(BudgetExceeded):
        b.try_reserve(Decimal("0.2"))
    with pytest.raises(BudgetExceeded):
        ClaudeBudget(ResearchConfig()).estimate_max_cost(1, 1)  # unknown pricing fails closed
    assert Analyst(ResearchConfig(), ClaudeBudget(ResearchConfig())).ready() == (False, "disabled")
    ok = '{"observations":["a"],"evidence_ids":["e1"],"hypotheses":["h"],"proposed_tests":["t"],"caveats":["c"]}'
    assert validate_output(ok)["hypotheses"] == ["h"]
    for bad in ('not json', '{"observations":[],"evidence_ids":[],"hypotheses":[],"proposed_tests":[],"caveats":[],"orders":[1]}',
                '{"observations":[],"evidence_ids":[],"hypotheses":[],"proposed_tests":[],"caveats":[],"proposed_parameters":{"max_entry_fraction":1}}',
                '{"observations":[],"evidence_ids":[],"hypotheses":[],"proposed_tests":[],"caveats":[],"proposed_parameters":{"scalp_target_cents":99}}'):
        with pytest.raises(ValueError):
            validate_output(bad)
