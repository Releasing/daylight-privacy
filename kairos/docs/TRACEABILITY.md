# Kairos — Requirements Traceability Checklist

Status legend: **DONE** implemented + automated test · **IMPL** implemented; final evidence needs the VPS or
live exchange data · **BLOCKED** cannot be completed from the build sandbox (reason given) · **GATED**
intentionally disabled until administrative authorization.

Test files: **C** `tests/unit/test_core.py` · **I** `tests/unit/test_intelligence.py` · **X**
`tests/unit/test_coverage_extra.py` · **R** `tests/unit/test_research_job.py` · **E**
`tests/integration/test_engine_api.py` · **F** `tests/faults/test_faults.py` · **UI** Playwright layout run
(`docs/screenshots/`). 108 automated tests pass (`cd backend && pytest -q`).

Module paths are relative to `backend/src/kairos/` unless prefixed `frontend/` or `deploy/`.

## §0 Superseding decisions applied

| Topic | Earlier | Superseded by | Applied rule |
|---|---|---|---|
| Transactional DB | SQLite WAL (p.5, p.40, p.90) | p.141 | PostgreSQL; SQLAlchemy Core keeps SQLite for unit tests only |
| Sizing tiers | 0.5%/position (p.16) | p.52, p.60, p.67, p.71 | Confidence tiers 1–5% × mode ceilings, immutable absolute caps |
| Confidence cutoffs | fixed tiers as permission (p.52) | p.87, p.101, p.107 | Confidence is diagnostic + size cap; never permission; no cutoff required beyond EV/risk gates; <50 → observe |
| Approval | operator approval of trades (p.17) | p.66, p.96, p.101 | No routine approvals; live enable / limit changes are administrative |
| Storage budget | 50 GB plan (p.40) | p.114 | 120 GB NVMe plan: 15 OS / 40 hist / 25 recent / 10 models / 5 app / 25 free |
| Navigation | sidebar (p.117), 5 mobile tabs (p.116) | p.129, p.137, p.138 | Exactly Markets · Trade · Community; settings/notifications utility icons; drawers for research/funds/history |
| Default chart | BTC/USD (p.118, p.126) | p.130, p.132 | Default = active Kalshi YES contract (cents); BTC reference view optional, separate axis |
| Users | single private operator (p.115) | p.129, p.137 | Multi-tenant architecture; owner account + test tenants; rollout capped |
| Platform | Python service + dashboard (p.1) | p.141–143 | React/Vite/Tailwind PWA + FastAPI |
| Late entries | entry cutoff (p.26) | p.106 | Configurable cutoff per strategy; settlement strategy may enter late if validated |
| Assets | BTC only | p.101, p.105 | BTC tradable; ETH/SOL read-only features, rejected at execution boundary |

## §1 Mandate and scope (p.1, 39, 51, 66, 78, 101, 113, 129)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Default PAPER; live disabled until approval + validation | 1, 4, 100 | `config.py`, `execution/orders.py::LiveAdapter`, `cli.py authorize-live` | C `test_config_defaults_safe`; I `test_paper_cannot_reach_live` | DONE / live GATED |
| Maximize net EV; no trade quota; abstain without edge | 1, 51, 106 | `engine/account.py`, `strategy/strategies.py` | E `test_rules_unverified_blocks_entries`; I `test_score_100_negative_ev_rejected` | DONE |
| Strategies A/B/C | 10–13 | `strategy/strategies.py` | I `test_settlement_candidates_both_sides`, `test_scalp_candidate_runs_and_is_conservative`, `test_arbitrage_requires_both_legs_and_valid_book` | DONE |
| No leverage / martingale / loss chasing | 1, 23, 57 | `risk/allocator.py` | I `test_no_size_increase_after_loss` | DONE |
| BTC allowlist; ETH/SOL rejected at execution | 101, 112 | `risk/allocator.py::hard_checks` | X `test_eth_market_rejected_at_execution_boundary` | DONE |
| No social / copy trading | 39, 82 | `config.py::validate` | C `test_config_rejects_unsafe_overrides` | DONE |
| Research cannot trade / change limits / see keys | 39, 42, 74 | frozen `RiskPolicy`; `kairos-research.service` (InaccessiblePaths secrets) | C `test_policy_frozen`, `test_config_rejects_unsafe_overrides` | DONE |
| External text instructions rejected | 39, 49 | `research/analyst.py::validate_output` | I `test_budget_and_output_validation` | DONE |

## §2 Contract mechanics, settlement, rollover (p.2, 26–27, 106, 133)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Discover from metadata; strike never from chart | 2, 133 | `market/discovery.py`, `kalshi/parse.py` | C `test_parse_market_dollars`, `test_parse_market_legacy_cents_and_missing` | DONE |
| Rules parsing, tie (≥ vs >), averaging window, template hash, operator verification | 2, 27 | `market/rules.py`, `cli accept-rules` | C `test_rules_parsing_and_template`; E `test_rules_unverified_blocks_entries` | DONE (template acceptance on VPS: IMPL) |
| `(60K−Σ)/(60−n)`; missing ≠ 0 | 2, 27 | `market/rules.py` | C `test_required_average`, `test_sampler_missing_not_zero` | DONE |
| Clock: close vs expiration; buckets 0-3/3-7/7-11/11-14/final | 2, 10, 26 | `market/rules.py::WindowClock` | C `test_clock` | DONE |
| Rollover, old positions keyed by old ticker | 133 | `engine/market.py::_rollover` | E `test_rollover_keeps_old_positions` | DONE |
| Settlement exactly once; mismatch disables model | 2, 26 | `engine/account.py::on_settlement`, `engine/market.py::settlement_mismatch` | E `test_paper_entry_settlement_and_restart`; F `test_close_during_pending_exit_settles_exactly_once`; X `test_settlement_mismatch_rule` | DONE |

## §3 Market data and integrity (p.3, 6, 29, 46–47, 105, 133)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Kalshi REST: series, markets, book, trades, candles, settlements | 3, 133 | `kalshi/rest.py`, `engine/market.py` | F `test_read_backoff_on_429_then_success`; E (fake REST) | IMPL — live schema confirmation via `kairos verify-api` on VPS (sandbox blocked) |
| Kalshi WS snapshot+delta; gap → stale + resubscribe | 6, 133 | `kalshi/ws.py`, `market/book.py` | C `test_seq_gap_invalidates` | IMPL — needs API key + live WS on VPS |
| YES ask = 1 − NO bid; missing ≠ 0; crossed rejected | 13, 133 | `market/book.py` | C `test_complement`, `test_crossed_book_rejected` | DONE |
| Coinbase + Kraken BTC/ETH/SOL | 3, 105 | `feeds/coinbase.py`, `feeds/kraken.py` | C `test_coinbase_kraken_normalize` | IMPL — live connection untestable in sandbox |
| Composite median; labelled exchange proxy | 3, 132 | `feeds/composite.py` | C `test_composite_median_and_staleness` | DONE |
| Feed health → entry blockers; auxiliary → BTC-only fallback | 3, 46, 105 | `integrity/health.py`, `features/crossasset.py` | E `test_stale_feed_blocks_and_pause_preserves_exits`; X `test_crossasset_fallback` | DONE |
| Liquidity integrity (cancel/add, persistence) | 46–47 | `integrity/health.py::LiquidityIntegrity` | used in confidence penalty | IMPL |
| Basis proxy→index learned from settlements | 3, 8 | `models/settlement.py::BasisTracker` | — | IMPL (needs `expiration_value` field on VPS) |

## §4 Keys, auth, secrets (p.4, 37, 49, 115, 137)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| RSA-PSS SHA-256 over ts+METHOD+path (no query) | 4 | `kalshi/auth.py` | C `test_sign_verify`, `test_signing_path_strips_query`; F `test_signed_request_headers_and_path` | DONE |
| Key files 0600; no secrets in logs/frontend | 4, 49 | `kalshi/auth.py`, `security/vault.py` | C `test_key_permission_enforced`; X `test_no_secret_in_logs`; E (snapshot has no secrets) | DONE |
| Per-tenant encrypted credentials | 137 | `security/vault.py` (AES-GCM, AAD per user) | X `test_vault_roundtrip_and_tamper` | DONE |
| 401/429/timeout/409; no blind write retry | 4, 15 | `kalshi/rest.py` | F `test_timeout_after_submission_is_unknown_not_retried`, `test_duplicate_client_id_409_and_429_and_401` | DONE |
| Ed25519 keys (docs mention) | 4 | — | — | Not implemented (spec mandates RSA-PSS) |

## §5 Architecture, storage, replay (p.5–6, 40–41, 88–92, 114)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Shared proposal / intent schema paper & live | 5 | `contracts.py` | I `test_paper_cannot_reach_live` | DONE |
| Order state machine; HALTED/PAUSED reachable | 5, 23 | `execution/orders.py::Journal`, `risk/mode.py` | I `test_journal_persist_before_submit_and_idempotency`, `test_mode_controller` | DONE |
| Raw Parquet zstd by UTC day/venue; checksums; manifest | 6, 40 | `storage/recorder.py` | I `test_recorder_checksums` | DONE |
| 60/70/80/90 % pressure; oldest-first pruning | 40–41, 114 | `storage/recorder.py`, `kairos storage-maintain` | I `test_thresholds`; X `test_prune_order_oldest_first`; F `test_disk_full_triggers_write_failure_halt` | DONE |
| Deterministic replay by receipt time; ledger hash | 5, 6, 92 | `replay/engine.py` | F `test_replay_deterministic_ledger_hash`; I `test_mc_deterministic_seed_and_monotone_strike` | DONE (historical replay needs recorded data) |
| Restart reconstructs from durable records, starts PAUSED | 18, 41, 77 | `engine/account.py::restore` | E `test_paper_entry_settlement_and_restart` | DONE |
| Three lanes; exits before entries; research separate process | 89, 91 | `engine/account.py`, `kairos-research.service` | E `test_stale_feed_blocks_and_pause_preserves_exits` | DONE |
| p50/p95/p99 latency | 48, 91 | `supervisor/latency.py` | ENGINEERING benchmarks | IMPL (VPS benchmark pending) |

## §6 Features, chart intelligence (p.7, 54–55, 104–105, 123–125, 128)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Returns 1–300 s, Theil–Sen trend, realized var | 7 | `features/indicators.py` | I `test_rolling_returns_out_of_order_and_gaps` | DONE |
| Imbalance, depth imbalance, microprice, spread | 7 | `features/flow.py`, `market/book.py` | I `test_trade_flow_zero_denominator`; C `test_microprice` | DONE |
| EMA/RSI/MACD/ATR/BB/VWAP golden fixtures, EWMA + jump | 54 | `features/indicators.py` | I `test_golden_ema_rsi_macd_atr_bb_vwap` | DONE |
| Completed vs provisional candles; out-of-order rejected | 55 | `features/candles.py` | I `test_no_unfinished_candle_leak` | DONE |
| Causal swings; detection ≠ pivot time | 124 | `levels/detect.py` | I `test_causal_swings_and_detection_time` | DONE |
| Clustering max(tick, 0.15 ATR); strength 0–100 | 124 | `levels/detect.py` | I `test_clustering_tolerance_and_strength_bounds` | DONE |
| Bounce/rejection/breakout/breakdown/false breakout/retest/role reversal | 125, 128 | `levels/patterns.py` | I `test_patterns_bounce_breakout_false_breakout`, `test_role_reversal_after_break_and_retest` | DONE |
| Overlay toggles never change inputs | 125, 128 | `frontend/src/state/prefs.ts` (display-only) | I `test_overlay_independent` | DONE |
| Multi-timeframe 1m/5m/15m/1h/4h + ETH/SOL lag features | 55, 105 | `features/candles.py`, `features/crossasset.py` | X `test_crossasset_fallback` | DONE |

## §7 Probability, prediction, learning (p.8–9, 42–45, 58, 83–84)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Φ baseline; MC averaged settlement; T=0/σ=0/monotone/determined/seeded | 8 | `models/settlement.py` | I `test_mc_*`, `test_phi_baseline_units` | DONE |
| Heavy-tail & block-bootstrap challengers; MC SE; conservative p | 8, 84 | `models/settlement.py` | I `test_mc_challengers_and_conservative` | DONE |
| Abstain on poor benchmark coverage | 8 | `models/settlement.py` | I `test_mc_missing_benchmark_abstains_and_invalid_inputs` | DONE |
| Multi-horizon 1/5/15/30/60/180 s | 83 | `models/horizons.py` | X `test_horizons_respect_remaining_time` | DONE (learned horizon models: shadow future work) |
| Brier/log loss/reliability/ECE | 9, 58 | `models/calibration.py` | I `test_calibration_metrics` | DONE |
| Grouped chronological splits, purge, embargo, leakage rejection | 9, 43 | `research/stats.py` | I `test_leakage_rejected_and_split` | DONE |
| LR + LightGBM shadow, signed registry, rollback | 42 | `research/learning.py`, `research/job.py` | I `test_registry_signature_and_promotion`; R `test_job_insufficient_then_trains` | DONE |
| Promotion gates reject high-win-rate loser, leaky, worse stressed DD | 44, 96 | `research/learning.py::evaluate_candidate` | I `test_promotion_rejects_bad_models` | DONE |

## §8 Economics, sizing, exits (p.10–14, 21–24, 52–64, 79–87, 103)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Fee adapter, maker/taker, multiplier, per-order rounding, unknown → disable | 14 | `money.py` | C `test_worked_scalp_fees`, `test_strict_bankroll_example`, `test_maker_fee_and_types`, `test_unknown_schedule_disables`, `test_fee_accumulator_rounds_per_order` | DONE |
| Settlement EV both sides; case A rejected | 10, 59, 79 | `strategy/economics.py` | I `test_per_contract_ev_and_case_a` | DONE |
| Scalp first passage on executable bid, depth-limited exit, stress | 11, 24, 30, 57, 84 | `strategy/strategies.py::simulate_scalp`, `replay/engine.py::path_label` | I `test_scalp_candidate_runs_and_is_conservative`; F `test_path_labels_and_counterfactuals` | DONE |
| Confidence 85.4 example; required inputs | 52–53 | `strategy/confidence.py` | I `test_confidence_example_and_required` | DONE |
| Kelly 40/20/10 %, scenario log growth, invalid → 0 | 61 | `strategy/economics.py` | I `test_kelly_examples` | DONE |
| Largest feasible integer q, never round up | 60, 63 | `strategy/economics.py` | I `test_largest_feasible_quantity_never_rounds_up` | DONE |
| Optimal stopping sell all/part/hold; risk override; stale fallback | 85–86 | `strategy/optimal_exit.py` | I `test_exit_decisions` | DONE |
| Edge registry lifecycle with hysteresis | 102, 109 | `strategy/registry.py` | I `test_registry_hysteresis` | DONE |
| Missed-opportunity records; counterfactual exits; oracle diagnostic only | 108, 92 | `db.decisions`, `replay/engine.py` | F `test_path_labels_and_counterfactuals` | DONE |

## §9 Risk (p.16, 35, 60, 63, 67, 71, 74, 107)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Score 100 + negative EV / stale → no order | 52, 35 | `risk/allocator.py` | I `test_score_100_negative_ev_rejected`, `test_stale_quote_and_halts_reject` | DONE |
| Caps: mode, tier, Kelly, 5 %, cash, market/portfolio/loss/turnover | 60, 63, 71 | `risk/allocator.py` | I `test_allocator_caps_and_tiers` | DONE |
| Concurrent proposals never overspend | 21, 72 | `risk/allocator.py` | I `test_concurrent_reservations_never_overspend`; E `test_manual_and_bot_share_funds` | DONE |
| Daily loss / drawdown | 16, 74 | `risk/allocator.py` | I `test_daily_loss_and_drawdown` | DONE |
| Mode controller; aggressive disabled; stepwise upgrade | 67 | `risk/mode.py` | I `test_mode_controller` | DONE |
| Halts auto/daily/latched; acknowledgement needs reconciliation | 16, 74 | `risk/halts.py` | I `test_halts` | DONE |
| Pause ≠ cancel ≠ flatten; exits continue when paused | 115 | `engine/account.py` | E `test_stale_feed_blocks_and_pause_preserves_exits`, `test_api_auth_csrf_tenant_and_community` | DONE |

## §10 Execution (p.12, 15, 22, 48, 72)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Journal before submit; unique client IDs; unknown ≠ rejected | 15 | `execution/orders.py` | I `test_journal_persist_before_submit_and_idempotency`; F `test_dropped_ack_and_duplicate_fill_events` | DONE |
| Paper fills consume visible depth once; touch ≠ fill; resting needs trade-through | 15, 30 | `execution/orders.py::PaperExchange` | I `test_paper_fill_consumes_depth_once`, `test_resting_needs_trade_through` | DONE |
| Reconciliation mismatch → halt | 12, 15 | `execution/reconcile.py` | I `test_reconcile_mismatch_and_markouts` | DONE (live exchange reconciliation loop: IMPL, needs live key) |
| Markouts 1/5/30 s | 48 | `execution/reconcile.py::Markouts` | I `test_reconcile_mismatch_and_markouts` | DONE |
| Live order body / V2 path | 15, 135 | `execution/orders.py::LiveAdapter` | — | GATED; schema unverified (sandbox) |

## §11 Ledger and finance (p.21–22, 68–72, 76)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Lots $15.75 / avg 35¢; partial exits $9; −$10; no oversell; fee allocation; netting | 21–22, 31–32 | `ledger/lots.py` | I `test_three_lots`, `test_partial_exits_and_duplicates`, `test_no_oversell_and_fee_allocation`, `test_netting_buy_no_closes_yes` | DONE |
| HWM table; balanced $10/$8/$2; residual to savings; policy moves | 68–70 | `finance/accounts.py` | I `test_hwm_table`, `test_balanced_split_and_protected_excluded`, `test_rounding_residual_to_savings_and_policy_moves` | DONE |
| Append-only ledger schema | 76 | `db/schema.py` | E (restart reconstruction) | DONE |

## §12 Optional Claude research (p.19, 45, 93–95)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Disabled by default; core independent | 19, 45, 99 | `research/analyst.py` | F `test_ai_unavailable_does_not_affect_trading` | DONE |
| $10 cap / $8 cutoff / $2 reserve; unknown price fails closed | 19, 94 | `research/analyst.py::ClaudeBudget` | I `test_budget_and_output_validation` | DONE |
| Schema-validated output; hypothesis lifecycle | 95 | `research/analyst.py` | I `test_budget_and_output_validation` | DONE (API transport intentionally not wired; enabling is an admin decision) |

## §13 Deployment (p.18, 37, 41, 113–115, 139, 143)

| Requirement | Page | Artifact | Status |
|---|---|---|---|
| Read-only VPS + DNS inspection | 113, 139 | `deploy/inspect_vps.sh` | IMPL — BLOCKED from sandbox (no SSH); operator runs |
| Separate account `kairosv11`, dirs, DB role; no overwrite of existing apps | 139 | `deploy/provision.sh` | IMPL — operator runs |
| Caddy HTTPS/ACME, CSP, same-origin API/WS | 115, 141 | `deploy/Caddyfile` | IMPL |
| systemd restart limits, resource controls, low-priority research, timers | 18, 41, 114 | `deploy/systemd/*` | IMPL |
| Encrypted backups, restore drill, rollback | 18, 41 | `deploy/backup.sh`, `restore_drill.sh`, `rollback.sh` | IMPL |

## §14 Interface (p.116–122, 126–138, 142–143)

| Requirement | Page | Artifact | Evidence | Status |
|---|---|---|---|---|
| Exactly three primary tabs | 129, 137 | `frontend/src/App.tsx` | UI: tabs = Markets, Trade, Community | DONE |
| V11 palette & typography | 117, 131 | `tailwind.config.ts`, `index.css` | screenshots | DONE |
| Desktop terminal: KPI strip, chart ~65 %, decision + book 35 %, bottom position/countdown/activity, status bar | 132 | `views/Markets.tsx` | UI 1366×768 / 1920×1080 / 2560×1440: no page scroll | DONE |
| Chart: YES ¢ default, NO, BTC reference $ with strike, S1/S2/R1/R2, zones, EMA, volume, patterns, fills, avg entry, targets; source + timestamp; no mixed units | 123, 128, 132 | `components/MarketChart.tsx` | screenshots | DONE |
| Decision panel separates probability, confidence, net edge, action, reasons, expandable diagnostics | 132 | `components/panels.tsx` | screenshots | DONE |
| Trade ticket: rules, expiry, bid/ask, depth, qty, limit, fee, max loss, payout, review + confirm; bottom sheet on iPhone | 135 | `views/Trade.tsx` | screenshots | DONE |
| Community: Today/7d/30d/All, paper/live, opt-in, empty state | 136 | `views/Community.tsx` | E community assertions; screenshots | DONE |
| Animations 100–250 ms, interruptible, reduced motion; server-synced countdown | 134 | `components/ui.tsx::AnimatedNumber`, `state/store.ts` | code review | DONE |
| PWA manifest, 192/512/apple icons, standalone, safe areas, ≥44 px targets, static-only SW | 116, 137, 142 | `public/*`, `sw.js` | UI: iPhone 390/393 + 768 no h-overflow, no targets < 32 px | DONE (actual iPhone install: IMPL on device) |
| Push (VAPID, no paid provider) | 116 | `api/push.py`, `sw.js` | R `test_vapid_and_notifier_summary` | IMPL (needs HTTPS + device) |
| Stale/offline labels, reconnect with snapshot on seq gap, resume refresh | 116, 134, 143 | `state/store.ts` | E `test_ws_requires_auth_and_origin` | DONE |
| Funds / Research / Settings / Activity drawers | 120–121 | `drawers/Drawers.tsx` | — | DONE |

## §15 Multi-user and community (p.129, 136–138)

| Requirement | Page | Module | Evidence | Status |
|---|---|---|---|---|
| Tenant isolation; cross-tenant denial | 137 | `api/app.py`, `db/store.py` | E `test_api_auth_csrf_tenant_and_community` | DONE |
| HttpOnly SameSite cookies, CSRF, rate limits, MFA (TOTP), re-auth, audit, WS origin + auth | 115, 137 | `api/app.py`, `security/vault.py` | E `test_api_auth_csrf_tenant_and_community`, `test_login_rate_limited`, `test_ws_requires_auth_and_origin` | DONE (passkeys not implemented; TOTP satisfies MFA) |
| Leaderboard metrics, opt-in, revocation, paper/live separated, no fake users | 136 | `community/leaderboard.py` | E `test_api_auth_csrf_tenant_and_community` | DONE |
| Manual vs bot origin; shared buying power; bot manages manual only if enabled | 135 | `engine/account.py` | E `test_manual_and_bot_share_funds` | DONE |
| User cap until load tested | 137 | `config.max_users`, `cli create-user` | — | DONE (load test on VPS: IMPL) |

## §16 Open questions / unresolved (must be answered on the VPS or by the operator)

1. Exact current Kalshi field names (`*_dollars`, `*_fp`) and the V2 order path (`/portfolio/events/orders`
   vs legacy `/portfolio/orders`) — parser accepts both; `kairos verify-api` records which is live.
2. Whether `KXBTC15M` "price to beat" is the open-minute BRTI average exposed as `floor_strike` (or similar) —
   parsed from market metadata; if absent the market is not tradable (fail closed).
3. Crypto-series fee multiplier / maker fees — read from `GET /series/{ticker}` (`fee_type`,
   `fee_multiplier`); unknown → trading disabled (p.14).
4. CF Benchmarks BRTI entitlement: not licensed → proxy only, uncertainty tracked.
5. Legal eligibility of other users before multi-user rollout (p.137) — operator decision.
