"""Strategy A (settlement), B (scalp), C (matched arbitrage) candidate generation (spec p.10–13, p.56–57,
p.79, p.102–103). Strategies only *propose*; the independent risk gate decides (p.39, p.107).

Each strategy declares regime, horizon, entry, target, stop, cutoff, probability model, fill assumptions
and exit policy (p.56) through the ``Candidate`` fields and ``STRATEGY_CARDS``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np

from ..contracts import Candidate
from ..market.book import OrderBook
from ..models.settlement import SettlementEstimate
from ..money import ONE, FeeSchedule, D, quadratic_fee_float
from . import confidence as conf
from .economics import kelly_binary, kelly_scenarios, settlement_entry
from .fairvalue import fair_yes

STRATEGY_CARDS = {
    "settlement": {
        "regime": "any validated", "horizon": "to settlement", "entry": "taker at executable ask (IOC limit)",
        "target": "settlement $1", "stop": "model deterioration via optimal-exit", "cutoff": "market close",
        "probability_model": "MC averaged settlement (models.settlement)", "fills": "visible depth after latency",
        "exit": "optimal stopping vs continuation value",
    },
    "scalp": {
        "regime": "trend/momentum or reversal", "horizon": "≤ timeout (default 300 s)",
        "entry": "taker at executable ask", "target": "+target cents on executable bid",
        "stop": "−stop cents on executable bid (not guaranteed)", "cutoff": "entry_cutoff_seconds before close",
        "probability_model": "first-passage path simulation (strategy.strategies.simulate_scalp)",
        "fills": "exit limited to visible bid depth; remainder valued at fair", "exit": "target/stop/timeout",
    },
    "arbitrage": {
        "regime": "any", "horizon": "to settlement", "entry": "both legs IOC", "target": "$1 per matched pair",
        "stop": "unwind leftover leg", "cutoff": "market close", "probability_model": "none (payout identity)",
        "fills": "both legs at visible depth; one-leg failure simulated", "exit": "settlement / netting",
    },
}


@dataclass
class MarketContext:
    ticker: str
    now: float
    close_ts: float
    strike: float
    spot: float | None  # composite proxy (label: exchange proxy)
    sigma_log_per_s: float | None
    drift_log_per_s: float
    book: OrderBook
    fees: FeeSchedule
    estimate: SettlementEstimate | None
    obs_sum: float = 0.0
    n_obs: int = 0
    basis_sd: float = 15.0
    trend_sigma_per_min: float | None = None
    flow_imbalance: float | None = None
    mtf_agreement: float | None = None
    vol_regime: float | None = None
    jump: bool = False
    liquidity_penalty: float = 0.0
    quote_age_ms: float | None = None
    features_hash: str = ""
    model_version: str = "settlement_mc_v1"
    reason_codes: list[str] = field(default_factory=list)

    @property
    def seconds_left(self) -> float:
        return self.close_ts - self.now

    def z_to_strike(self) -> float | None:
        if self.spot is None or not self.sigma_log_per_s or self.seconds_left <= 0:
            return None
        sd = self.spot * self.sigma_log_per_s * math.sqrt(max(self.seconds_left - 40.0, 1.0))
        return (self.spot - self.strike) / sd if sd > 0 else None


def _confidence(ctx: MarketContext, side: str, ev_per_dollar: float | None, depth_ok: bool, scalp_tbs: float | None = None) -> conf.ConfidenceResult:
    sp = ctx.book.spread_yes()
    return conf.score(conf.ConfidenceInputs(
        side=side, z_to_strike=ctx.z_to_strike(), trend=ctx.trend_sigma_per_min, flow_imbalance=ctx.flow_imbalance,
        ev_per_dollar=ev_per_dollar, mtf_agreement=ctx.mtf_agreement, spread=float(sp) if sp is not None else None,
        quote_age_ms=ctx.quote_age_ms, depth_ok=depth_ok, vol_regime=ctx.vol_regime, jump=ctx.jump,
        liquidity_penalty=ctx.liquidity_penalty, target_before_stop=scalp_tbs), scalp=scalp_tbs is not None)


# ------------------------------------------------------------------------------------------------
# Strategy A — settlement
# ------------------------------------------------------------------------------------------------
def settlement_candidates(ctx: MarketContext, probe_qty: int = 1, max_qty: int = 500) -> list[Candidate]:
    out: list[Candidate] = []
    est = ctx.estimate
    if est is None or not est.valid:
        return out
    for side in ("yes", "no"):
        p = est.p_yes if side == "yes" else 1 - est.p_yes
        pc = est.p_conservative_yes if side == "yes" else est.p_conservative_no
        ask = ctx.book.best_yes_ask() if side == "yes" else ctx.book.best_no_ask()
        if ask is None or ask <= 0 or ask >= 1:
            continue
        econ = settlement_entry(ctx.book, side, Decimal(probe_qty), p, pc, ctx.fees)
        depth_ok = econ is not None
        evpc = (econ.ev_conservative / probe_qty) if econ else Decimal("-1")
        fee_pc = float(econ.fee / probe_qty) if econ else 0.0
        c = _confidence(ctx, side, econ.ev_per_dollar if econ else None, depth_ok)
        reasons = list(ctx.reason_codes)
        if econ is None:
            reasons.append("insufficient_depth")
        if evpc <= 0:
            reasons.append("nonpositive_conservative_ev")
        out.append(Candidate(
            strategy_id="settlement", ticker=ctx.ticker, side=side, action="buy", max_qty=max_qty,
            limit_price=ask, p_point=p, p_conservative=pc, ev_per_contract=evpc,
            worst_loss_per_contract=(ask + D(round(fee_pc, 4))), confidence=c.score,
            confidence_components=c.components, kelly_fraction=kelly_binary(pc, float(ask), fee_pc),
            horizon_s=int(max(ctx.seconds_left, 0)), model_version=f"{ctx.model_version}/{est.model}",
            feature_hash=ctx.features_hash, reason_codes=reasons, cutoff_ts=ctx.close_ts,
            hold_seconds=max(ctx.seconds_left, 1.0), created_ts=ctx.now,
            scenarios={"p_low": est.p_low, "p_high": est.p_high, "mc_se": est.mc_se},
        ))
    return out


# ------------------------------------------------------------------------------------------------
# Strategy B — scalp (first passage on executable bid, p.11, p.24, p.30, p.57, p.84)
# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ScalpOutcome:
    p_target: float
    p_stop: float
    p_timeout: float
    ev_per_contract: float
    exit_mean: dict[str, float]
    returns: dict[str, float]  # per-dollar net returns by outcome (for log-growth sizing)
    n_paths: int


def simulate_scalp(ctx: MarketContext, side: str, entry: float, target_c: int, stop_c: int, horizon_s: int,
                   qty: int, exit_depth: float, n_paths: int = 800, seed: int = 7, vol_mult: float = 1.0,
                   spread_mult: float = 1.0, drift_mult: float = 1.0) -> ScalpOutcome | None:
    if ctx.spot is None or not ctx.sigma_log_per_s or horizon_s <= 0:
        return None
    rng = np.random.default_rng(seed)
    sig = ctx.sigma_log_per_s * vol_mult
    mu = ctx.drift_log_per_s * drift_mult
    steps = int(horizon_s)
    z = rng.standard_normal((n_paths, steps))
    S = ctx.spot * np.exp(np.cumsum(mu + sig * z, axis=1))
    t = ctx.now + np.arange(1, steps + 1)
    R = ctx.close_ts - t
    fy = fair_yes(S, ctx.strike, R[None, :], ctx.spot * sig, ctx.obs_sum, ctx.n_obs, ctx.basis_sd)
    sp = float(ctx.book.spread_yes() or Decimal("0.02")) * spread_mult
    fair_side = fy if side == "yes" else 1.0 - fy
    bid = np.clip(np.floor((fair_side - sp / 2.0) * 100.0) / 100.0, 0.0, 0.99)  # executable bid on cent grid
    tgt, stp = entry + target_c / 100.0, entry - stop_c / 100.0
    hit_t = bid >= tgt - 1e-9
    hit_s = bid <= stp + 1e-9
    first_t = np.where(hit_t.any(axis=1), hit_t.argmax(axis=1), steps)
    first_s = np.where(hit_s.any(axis=1), hit_s.argmax(axis=1), steps)
    target_first = first_t < first_s
    stop_first = first_s < first_t
    timeout = ~(target_first | stop_first)
    rows = np.arange(n_paths)
    exit_idx = np.where(target_first, first_t, np.where(stop_first, first_s, steps - 1))
    exit_bid = bid[rows, exit_idx]
    # stop is not guaranteed: price can gap through — assume one extra cent of slippage on stops
    exit_px = np.where(stop_first, np.maximum(exit_bid - 0.01, 0.0), exit_bid)
    # exit limited to visible bid depth; the remainder is held and valued at fair (residual settlement exposure)
    fill_frac = min(1.0, max(0.0, exit_depth / max(qty, 1)))
    resid_val = fair_side[rows, exit_idx]
    entry_fee = quadratic_fee_float(qty, entry) / qty
    exit_fee = np.array([quadratic_fee_float(max(1, round(qty * fill_frac)), float(p)) for p in np.unique(exit_px)])
    fee_map = dict(zip(np.unique(exit_px).tolist(), (exit_fee / max(1, round(qty * fill_frac))).tolist()))
    ex_fee = np.vectorize(lambda p: fee_map.get(float(p), 0.0))(exit_px) * fill_frac
    proceeds = fill_frac * exit_px + (1 - fill_frac) * resid_val - ex_fee
    net = proceeds - entry - entry_fee
    stake = entry + entry_fee
    res = {}
    for name, m in (("target", target_first), ("stop", stop_first), ("timeout", timeout)):
        res[name] = float(net[m].mean() / stake) if m.any() else 0.0
    return ScalpOutcome(
        p_target=float(target_first.mean()), p_stop=float(stop_first.mean()), p_timeout=float(timeout.mean()),
        ev_per_contract=float(net.mean()),
        exit_mean={"target": float(exit_px[target_first].mean()) if target_first.any() else float("nan"),
                   "stop": float(exit_px[stop_first].mean()) if stop_first.any() else float("nan"),
                   "timeout": float(exit_px[timeout].mean()) if timeout.any() else float("nan")},
        returns=res, n_paths=n_paths,
    )


def scalp_candidates(ctx: MarketContext, target_c: int, stop_c: int, timeout_s: int, cutoff_s: int, qty: int = 10,
                     max_qty: int = 500, seed: int = 11) -> list[Candidate]:
    out: list[Candidate] = []
    horizon = int(min(timeout_s, ctx.seconds_left - cutoff_s))
    for side in ("yes", "no"):
        reasons = list(ctx.reason_codes)
        ask = ctx.book.best_yes_ask() if side == "yes" else ctx.book.best_no_ask()
        bids = ctx.book.bid_levels(side, 1)
        if ask is None or not bids:
            continue
        if horizon <= 5:
            reasons.append("scalp_after_cutoff")
            continue
        f = ctx.book.buy(side, Decimal(qty))
        if f.quantity < qty or f.vwap is None:
            reasons.append("insufficient_depth")
            continue
        entry = float(f.vwap)
        exit_depth = float(bids[0][1])
        base = simulate_scalp(ctx, side, entry, target_c, stop_c, horizon, qty, exit_depth, seed=seed)
        stress = simulate_scalp(ctx, side, entry, target_c, stop_c, horizon, qty, exit_depth, seed=seed,
                                vol_mult=1.25, spread_mult=1.5, drift_mult=0.5)
        if base is None or stress is None:
            reasons.append("scalp_model_unavailable")
            continue
        evc = min(base.ev_per_contract, stress.ev_per_contract)
        if evc <= 0:
            reasons.append("nonpositive_conservative_ev")
        probs = [stress.p_target, stress.p_stop, stress.p_timeout]
        rets = [stress.returns["target"], stress.returns["stop"], stress.returns["timeout"]]
        kf = kelly_scenarios(probs, rets) if abs(sum(probs) - 1) < 1e-6 else 0.0
        c = _confidence(ctx, side, evc / (entry + 1e-9), True, scalp_tbs=stress.p_target / max(stress.p_target + stress.p_stop, 1e-9))
        out.append(Candidate(
            strategy_id="scalp", ticker=ctx.ticker, side=side, action="buy", max_qty=max_qty,
            limit_price=D(round(entry, 4)) if f.worst is None else f.worst, p_point=base.p_target,
            p_conservative=stress.p_target, ev_per_contract=D(round(evc, 4)),
            worst_loss_per_contract=D(round(entry, 4)) + D(round(quadratic_fee_float(qty, entry) / qty, 4)),
            confidence=c.score, confidence_components=c.components, kelly_fraction=kf, horizon_s=horizon,
            model_version="scalp_fp_v1", feature_hash=ctx.features_hash, reason_codes=reasons,
            target=D(round(entry + target_c / 100, 4)), stop=D(round(max(entry - stop_c / 100, 0.01), 4)),
            cutoff_ts=ctx.close_ts - cutoff_s, hold_seconds=float(horizon) / 2, created_ts=ctx.now,
            scenarios={"p_target": base.p_target, "p_stop": base.p_stop, "p_timeout": base.p_timeout,
                       "stress_p_target": stress.p_target, "ev_base": base.ev_per_contract, "ev_stress": stress.ev_per_contract},
        ))
    return out


# ------------------------------------------------------------------------------------------------
# Strategy C — matched arbitrage (p.13, p.34)
# ------------------------------------------------------------------------------------------------
def arbitrage_candidate(ctx: MarketContext, qty: int, buffer: Decimal = Decimal("0.01")) -> Candidate | None:
    """Gross = 1 − askYES − askNO per pair at executable depth. With Kalshi's complementary bid-only books
    this is positive only when the book is crossed (which integrity rejects), so this strategy is expected
    to be inactive on a single valid book; it remains implemented for verification and research (p.13)."""
    fy = ctx.book.buy("yes", Decimal(qty))
    fn = ctx.book.buy("no", Decimal(qty))
    if fy.quantity < qty or fn.quantity < qty or fy.vwap is None or fn.vwap is None:
        return None
    fees = ctx.fees.fee(qty, fy.vwap) + ctx.fees.fee(qty, fn.vwap)
    net = Decimal(qty) * ONE - fy.cost - fn.cost - fees - buffer * qty
    reasons = list(ctx.reason_codes)
    if net <= 0:
        reasons.append("no_net_arbitrage")
    return Candidate(
        strategy_id="arbitrage", ticker=ctx.ticker, side="yes", action="buy", max_qty=qty, limit_price=fy.worst or fy.vwap,
        p_point=1.0, p_conservative=1.0, ev_per_contract=net / qty, worst_loss_per_contract=fy.vwap + fees / qty,
        confidence=None, confidence_components={}, kelly_fraction=0.0, horizon_s=int(ctx.seconds_left),
        model_version="arb_identity_v1", feature_hash=ctx.features_hash, reason_codes=reasons,
        legs=[{"side": "yes", "vwap": str(fy.vwap), "qty": qty}, {"side": "no", "vwap": str(fn.vwap), "qty": qty}],
        hold_seconds=max(ctx.seconds_left, 1.0), created_ts=ctx.now,
        scenarios={"unmatched_exposure": float(fy.cost + ctx.fees.fee(qty, fy.vwap))},
    )


def rank(cands: list[Candidate]) -> list[Candidate]:
    """Rank by conservative marginal net EV per unit capital-minute (p.59, p.80)."""
    return sorted(cands, key=lambda c: c.ev_per_capital_minute, reverse=True)
