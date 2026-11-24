# Engineering Reference — formulas, strategy cards, model cards, data dictionary

All money is `Decimal` dollars; contract prices in [0,1] with ≤4 decimals; fees in whole cents.

## Formulas (with spec pages and the test that pins them)

| Quantity | Formula | Page | Test |
|---|---|---|---|
| Taker fee | `ceil_cent(0.07·m·C·P·(1−P))`, maker `0.0175` when `quadratic_with_maker_fees`; per-order accumulator across fills | 14 | `test_core::test_worked_scalp_fees`, `test_fee_accumulator_rounds_per_order` |
| YES ask | `1 − best NO bid` (bids-only books) | 13, 133 | `test_core::test_complement` |
| Final-minute requirement | `(60K − Σobs)/(60 − n)` | 2 | `test_required_average` |
| Microprice | `(ask·bid_q + bid·ask_q)/(bid_q+ask_q)` | 7 | `test_microprice` |
| Imbalance | `(buy−sell)/(buy+sell)`; zero denominator → missing | 7 | `test_trade_flow_zero_denominator` |
| EWMA variance | `λ·var + (1−λ)·r²`, floor, jump flag | 7 | `test_golden_*` |
| Baseline | `Φ((S−K+μT)/(σ√T))` (teaching only) | 8 | `test_phi_baseline_units` |
| Settlement p | MC over per-second paths + observed final-window samples + basis draw; SE `√(p(1−p)/N)` | 8 | `test_mc_*` |
| Conservative p | `min(p, p_stress_vol↑, p_vol↓) − 2·SE` (side-specific) | 9, 58 | `test_mc_challengers_and_conservative` |
| Settlement EV | `q(p − c) − fees` | 10, 79, 103 | `test_per_contract_ev_and_case_a` |
| Scalp EV | `Σ P_j·net_j` over target/stop/timeout incl. partial exit depth, residual exposure, fees | 11, 24, 57 | `test_scalp_candidate_runs_and_is_conservative` |
| Arbitrage | `1 − askY − askN − fees − buffer` per pair | 13 | `test_arbitrage_requires_both_legs_and_valid_book` |
| Confidence | `Σ wᵢ·sᵢ/100`, w = 25/20/15/15/10/10/5 | 52–53 | `test_confidence_example_and_required` (85.4) |
| Kelly | `max(0,(p·b−(1−p)a)/(a·b))`; scenario log-growth for scalps | 61 | `test_kelly_examples` (40/20/10%) |
| Allocation | `budget = min(E·mode, E·tier, E·k·kelly, E·5%, cash, market/portfolio/loss/turnover headroom)`; largest integer q | 63, 71 | `test_allocator_caps_and_tiers` |
| Mode | `min(health, market, exec, strategy, policy)`; veto → PAUSED; stepwise upgrade after dwell | 67 | `test_mode_controller` |
| High-water | `new = max(0, R−H)`, `H' = max(H, R)` | 70 | `test_hwm_table` |
| Buckets | balanced 50/40/10 etc., residual cents → savings | 68–69 | `test_balanced_split…` |
| Lot accounting | all-in avg `Σ(qa+f)/Σq`; FIFO, pro-rata entry fees | 21–22 | `test_three_lots`, `test_partial_exits_and_duplicates` |
| Uncertainty buffer | `z·σ·√(remaining min)` | 28 | `test_phi_baseline_units` |
| Leaderboard return | `net / (start_equity + Σ w·flows)` (modified Dietz) | 136 | `test_api_auth_csrf_tenant_and_community` |

## Strategy cards (p.56) — `strategy/strategies.py::STRATEGY_CARDS`

**A Settlement** — regime any validated; horizon to settlement; taker IOC at executable ask; target $1;
exit via optimal stopping (sell all / part / hold vs conservative continuation); model MC averaged settlement;
fills on visible depth after latency. Both sides evaluated every cycle.

**B Scalp** — first passage of the executable bid to `entry+target` before `entry−stop` or timeout; exit
limited to visible bid depth, remainder valued at fair (residual settlement exposure); 1¢ stop slippage;
conservative EV = min(base, stressed σ×1.25/spread×1.5/drift×0.5). Default no-drift baseline ⇒ scalps are
expected to show non-positive EV until a validated drift signal exists — this is intended, not a bug.

**C Matched arbitrage** — payout identity; with bids-only complementary books, `askY + askN = 2 − bidY − bidN`
is < 1 only on a crossed book, which integrity rejects. Implemented and logged for research; execution of
two-leg orders is disabled ("research only") until netting and leg-risk semantics are verified on the venue.

## Model cards

| Model | Status | Inputs | Output | Validation |
|---|---|---|---|---|
| `settlement_mc_v1/gaussian` | Champion (frozen) | composite proxy, EWMA σ, observed final-window samples, strike, rule, basis | P(YES), SE, band | unit properties; calibration vs settlements accrues in Research drawer |
| `student_t`, `bootstrap` | Challengers (shadow) | same | same | `test_mc_challengers_and_conservative` |
| `fair_value` closed form | Inner-loop approximation | — | fair YES | `test_fair_value_matches_mc` (<3 pp) |
| `ewma_gaussian_v1` horizons | Baseline | σ, drift (shrunk 50%) | P(up) at 1/5/15/30/60/180 s | — |
| LR / LightGBM | Shadow only (`kairos research`) | 10 features (job.FEATURES) | P(YES) | chronological grouped split, purge/embargo, Brier/logloss/ECE vs champion; never auto-promoted |

## Data dictionary (PostgreSQL, `db/schema.py`)

`users` (credentials hashed/encrypted, community opt-in, paper equity) · `sessions` (hashed token, CSRF,
re-auth time) · `audit_log` · `credentials` (AES-GCM blobs) · `orders` (journal; client_order_id PK; state)
· `fills` (dedup by fill_id) · `ledger_events` (append-only: fill/settle/deposit/settle_marker; realized P&L,
bucket deltas, policy version, reconciliation status, episode) · `funds_state` (materialized buckets) ·
`decisions` (every enter/exit/hold/skip with reasons, p, EV, confidence, size cap, model version, feature hash)
· `settlements` (official result, strike, proxy average, last model p) · `windows` (coverage) ·
`live_authorizations` · `push_subscriptions` · `kv` (verified rules templates, API fingerprint, reports).

Parquet: `data/market/raw/date=YYYY-MM-DD/venue=…/*.parquet` (raw schema p.6, zstd, SHA-256 manifest) and
`data/features/date=…/*.parquet` (one-second derived features + model p).

## Benchmarks (build sandbox, 4 vCPU x86-64 — re-measure on the VPS)

| Path | p50 | p95 | p99 |
|---|---|---|---|
| market view incl. MC 3×4,000 paths | — | 25.6 ms | 29.1 ms |
| account decision (exits + both-side scan + risk) | — | 70.6 ms | 85.8 ms |
| Frontend bundle | 433 kB JS (139 kB gzip), 18 kB CSS | | |

Disk: raw Parquet growth must be measured on the VPS (`kairos storage-maintain` logs usage hourly).
