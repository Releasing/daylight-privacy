# Kairos — Requirements Traceability Checklist

Status legend: **DONE** implemented + automated test · **IMPL** implemented, verified manually or needs
VPS/live data for final evidence · **BLOCKED** cannot be completed from the build sandbox (reason given) ·
**GATED** intentionally disabled until administrative authorization.

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

## §1 Mandate, scope and contract (p.1–2, 39, 51, 66, 78, 101, 113, 129)

| Req | Page | Module / artifact | Test | Status |
|---|---|---|---|---|
| Default PAPER; live disabled until explicit approval + validation | 1, 4, 39, 100 | `config.py`, `execution/live.py`, `execution/router.py` | `test_execution.py::test_paper_cannot_reach_live`, `test_config.py` | |
| Maximize net EV, no trade quota, abstain without edge | 1, 51, 78, 106 | `strategy/scanner.py` | `test_strategies.py::test_no_quota` | |
| Strategies A settlement, B scalp, C arbitrage | 1, 10–13 | `strategy/settlement.py`, `scalp.py`, `arbitrage.py` | `test_strategies.py` | |
| No leverage / martingale / averaging-down / loss chasing | 1, 21, 23, 57, 62 | `risk/gate.py` | `test_risk.py::test_no_size_increase_after_loss` | |
| BTC 15m series allowlist; ETH/SOL rejected at execution | 101, 112 | `execution/router.py` | `test_execution.py::test_allowlist` | |
| No social/sentiment/copy trading | 39, 51, 82 | `config.py` (`social_sources_enabled=false`, rejected if true) | `test_config.py` | |
| Research cannot trade, alter limits, see keys | 39, 42, 74, 109 | `risk/policy.py` (frozen), process separation | `test_risk.py::test_policy_immutable` | |
| External text instructions rejected | 39, 49 | `research/analyst.py` (schema allowlist) | `test_research.py` | |

## §2 Contract mechanics, settlement and rollover (p.2, 27, 82, 106, 133)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Discover series/markets from exchange metadata; never derive strike from chart | 2, 133 | `market/discovery.py`, `kalshi/parse.py` | `test_market.py::test_parse_market_*` | |
| Rules hash, tie policy, averaging window, missing samples | 2, 27 | `market/rules.py` | `test_market.py::test_rules_*` | |
| Final-minute remaining-average formula `(60K − Σ)/(60−n)`; missing ≠ 0 | 2, 27 | `market/rules.py::required_remaining_average` | `test_market.py::test_required_average` | |
| Clock model, trading close vs determination vs settlement | 2, 26 | `market/rules.py`, `market/clock.py` | `test_market.py::test_clock` | |
| Rollover: discover next ticker, atomic switch, old positions keyed by old ticker | 133 | `market/discovery.py`, `engine/core.py` | `test_engine.py::test_rollover` | |
| Settlement reconciliation vs official result; mismatch disables model | 2 | `engine/core.py::_on_settlement` | `test_engine.py::test_settlement_mismatch_halts` | |

## §3 Market data, feeds, integrity (p.3, 6, 29, 46–47, 105, 133)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Kalshi REST metadata, orderbook, trades, candles, settlements | 3, 133 | `kalshi/rest.py` | `test_kalshi.py` | |
| Kalshi WS snapshot+delta, seq gap → stale + resnapshot | 6, 29, 133 | `kalshi/ws.py`, `market/book.py` | `test_book.py::test_seq_gap` | |
| Complementary asks: YES ask = 1 − best NO bid; missing ≠ 0 | 13, 133 | `market/book.py` | `test_book.py::test_complement` | |
| Coinbase + Kraken BTC/ETH/SOL streams, heartbeat | 3, 105 | `feeds/coinbase.py`, `feeds/kraken.py` | `test_feeds.py` | |
| Composite median of midpoints; basis uncertainty; never claim official index | 3, 132 | `feeds/composite.py` | `test_feeds.py::test_composite_median` | |
| Feed health: lag, gaps, skew, dispersion, reason codes; stale blocks entries | 3, 46 | `integrity/health.py` | `test_integrity.py` | |
| Crossed books, one-venue spikes, depth persistence, cancellation intensity | 46, 47 | `integrity/liquidity.py` | `test_integrity.py` | |
| ETH/SOL outage → BTC-only fallback, not block | 105 | `features/crossasset.py` | `test_features.py::test_crossasset_fallback` | |

## §4 Keys, auth, secrets (p.4, 37, 49, 115, 137)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| RSA-PSS SHA-256 over ts+METHOD+path (query stripped) | 4 | `kalshi/auth.py` | `test_auth.py::test_sign_verify` | |
| Keys 0600, env points to secret paths, never logged/frontend | 4, 49 | `security/secrets.py`, log redaction filter | `test_security.py::test_no_secret_in_logs` | |
| Per-tenant encrypted credentials | 137 | `security/vault.py` (AES-GCM, server master key) | `test_security.py::test_vault_roundtrip` | |
| 401/429/timeout/duplicate client id; no blind write retry | 4, 15 | `kalshi/rest.py`, `execution/live.py` | `test_kalshi.py::test_retry_policy` | |

## §5 Architecture, storage, replay (p.5–6, 36, 40–41, 88–92, 114)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Pipeline separation, shared proposal schema paper/live | 5 | `contracts.py` | `test_execution.py` | |
| State machine DISCOVERED→…→CLOSED, HALTED from any | 5, 23 | `execution/session.py` | `test_execution.py::test_state_machine` | |
| Raw event schema; Parquet zstd by UTC day/venue; checksums+manifest | 6, 40 | `storage/recorder.py` | `test_storage.py` | |
| Retention tiers + pressure thresholds 60/70/80/90% | 40, 41, 114 | `storage/quota.py` | `test_storage.py::test_thresholds`, `test_prune_order` | |
| Replay by receipt time, deterministic, twice-identical | 5, 6, 92 | `replay/engine.py` | `test_replay.py::test_deterministic` | |
| Three-speed lanes; risk/exits never wait for research | 89, 91 | `engine/core.py`, `supervisor/latency.py` | `test_engine.py::test_exit_lane_not_blocked` | |
| p50/p95/p99 latency instrumentation | 48, 91 | `supervisor/latency.py` | `test_supervisor.py` | |

## §6 Features, chart intelligence, levels (p.7, 54–55, 104–105, 123–125, 128)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Returns 1/5/15/30/60/300 s, trend, strike distance in $ and σ | 7 | `features/snapshot.py` | `test_features.py` | |
| Trade imbalance, depth imbalance, microprice, spread | 7 | `features/flow.py` | `test_features.py::test_microprice` | |
| EWMA variance, realized vol, vol floor, jump flag | 7 | `features/indicators.py` | `test_indicators.py` | |
| EMA 9/21, VWAP, RSI14, MACD 12/26/9, ATR14, BB 20/2 golden fixtures | 54 | `features/indicators.py` | `test_indicators.py::test_golden_*` | |
| Multi-timeframe completed candles, provisional flag | 55, 105 | `features/candles.py` | `test_features.py::test_no_unfinished_candle_leak` | |
| Swing detection after right-side bars; detection time ≠ pivot time | 124 | `levels/detect.py` | `test_levels.py::test_causal_swings` | |
| Cluster tolerance max(tick, 0.15 ATR); strength 0–100; lifecycle | 124 | `levels/detect.py` | `test_levels.py` | |
| Bounce / rejection / breakout / breakdown / retest / false breakout | 125, 128 | `levels/patterns.py` | `test_levels.py::test_patterns_*` | |
| Overlay toggles never change model inputs | 125, 128 | UI-only prefs (`frontend/src/state/prefs.ts`) | `test_levels.py::test_overlay_independent` | |

## §7 Probability, prediction, learning (p.8–9, 42–45, 58, 83–84, 94–96)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Teaching baseline Φ(z) | 8 | `models/baseline.py` | `test_models.py` | |
| MC settlement with observed final-minute samples, verified rule | 8 | `models/settlement.py` | `test_models.py::test_mc_*` (units, T=0, σ=0, monotone strike, determined, seeded) | |
| Block-bootstrap / heavy-tail challengers; MC error √(p(1−p)/N) | 8, 84 | `models/settlement.py` | `test_models.py` | |
| Multi-horizon 1/5/15/30/60/180 s + settle | 83 | `models/horizons.py` | `test_models.py::test_horizons` | |
| Calibration: Brier, log loss, reliability bins, ECE | 9, 58 | `models/calibration.py` | `test_models.py::test_calibration` | |
| Chronological grouped splits, purge, embargo, leakage rejection | 9, 43, 44 | `research/splits.py` | `test_learning.py::test_leakage_rejected` | |
| LR + LightGBM champion/challenger shadow, registry w/ hashes | 42 | `research/learning.py`, `research/registry.py` | `test_learning.py` | |
| Promotion gates (30 d, 1,000 windows, LCB>0, stress); bad models fail | 44, 96 | `research/promotion.py` | `test_learning.py::test_promotion_rejects_*` | |
| Day-block bootstrap CIs | 9, 17 | `research/stats.py` | `test_learning.py::test_block_bootstrap` | |

## §8 Strategy economics, sizing, exits (p.10–14, 21–24, 52–64, 79–81, 85–87, 103)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Fee `ceil_cent(rate·C·P·(1−P))`, maker/taker, multiplier, versioned, unknown → disable | 14 | `money.py::FeeSchedule` | `test_fees.py` (worked $0.84/$0.79 examples) | |
| Settlement EV = p − ask − fees; both sides | 10, 79, 102 | `strategy/economics.py` | `test_strategies.py::test_settlement_ev` | |
| Scalp EV = Σ p_j·net_j incl. timeout/nonfill; first passage on executable bid | 11, 24, 57 | `strategy/scalp.py` | `test_strategies.py::test_scalp_*` | |
| Arbitrage gross = 1 − askY − askN; leg risk | 13, 34 | `strategy/arbitrage.py` | `test_strategies.py::test_arb_*` | |
| Confidence score Σw·s/100 (85.4 fixture); tiers; scalp variant | 52, 53 | `strategy/confidence.py` | `test_strategies.py::test_confidence_example` | |
| Attractiveness vs confidence (A rejected) | 59 | `strategy/scanner.py` | `test_strategies.py::test_case_A_rejected` | |
| Fractional Kelly (40/20/10/5 % example), log-growth for scalps | 61, 71 | `strategy/kelly.py` | `test_strategies.py::test_kelly_*` | |
| Optimal stopping: sell all / part / hold | 85 | `strategy/optimal_exit.py` | `test_strategies.py::test_exit_*` | |
| Edge registry shadow→eligible→active→reduced→suspended, hysteresis | 102, 109 | `strategy/registry.py` | `test_strategies.py::test_registry` | |
| Missed-opportunity decision records + counterfactual replay | 108 | `replay/counterfactual.py` | `test_replay.py::test_counterfactual` | |

## §9 Risk, modes, allocator, halts (p.16, 35, 60, 63, 67, 71, 74, 107)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Mode = min(health, market, exec, strategy, policy); hard veto → PAUSED; dwell before upgrade | 67 | `risk/mode.py` | `test_risk.py::test_mode_*` | |
| Aggressive disabled by default | 67, 80 | `risk/policy.py` | `test_risk.py` | |
| Atomic allocator `min(...)`, largest integer q, never round up | 63, 71 | `risk/allocator.py` | `test_risk.py::test_allocator_*` | |
| Reserve worst-case for pending + inventory; concurrent proposals never overspend | 16, 21, 72 | `risk/reservations.py` | `test_risk.py::test_concurrent_reservations` | |
| Halt triggers, latched vs auto-recoverable | 16, 74 | `risk/halts.py` | `test_risk.py::test_halts` | |
| Score 100 with negative EV / stale → no order | 52 | `risk/gate.py` | `test_risk.py::test_gate_*` | |
| Pause entries ≠ cancel pending ≠ emergency flatten; exits preserved | 115 | `engine/controls.py` | `test_engine.py::test_pause_preserves_exits` | |

## §10 Execution and reconciliation (p.12, 15, 22, 48, 72)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Intent journal persisted before submit, unique client IDs, unknown ≠ rejected | 15 | `execution/journal.py` | `test_execution.py::test_journal_*` | |
| Paper fills consume visible depth after latency; touched quote ≠ fill | 15, 30 | `execution/paper.py` | `test_execution.py::test_paper_fill_*` | |
| Partial fills change inventory; acks never do | 5 | `execution/journal.py` | `test_execution.py` | |
| Sell reservation; overlapping exits prevented | 22 | `ledger/lots.py` | `test_ledger.py::test_no_oversell` | |
| Reconcile on startup/periodic; mismatch halts | 12, 15 | `execution/reconcile.py` | `test_execution.py::test_reconcile_mismatch_halts` | |
| Markouts 1/5/30 s | 48 | `execution/markouts.py` | `test_execution.py::test_markouts` | |
| Fault injection: dropped ack, 429, duplicate fill, close during submit, restart | 15, 23, 98 | `tests/faults/` | `tests/faults/test_faults.py` | |

## §11 Ledger and finance (p.12, 21–22, 68–72, 76)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Lots, all-in cost basis (15@40/35/30 = $15.75) | 21, 31 | `ledger/lots.py` | `test_ledger.py::test_three_lots` | |
| Partial exits (20@50,20@60,10@70 = $9) | 22, 32 | `ledger/lots.py` | `test_ledger.py::test_partial_exits` | |
| High-water mark table (p.70) | 70 | `finance/high_water.py` | `test_finance.py::test_hwm_table` | |
| Profit buckets: balanced $20 → $10/$8/$2 once; adaptive policies | 68, 69 | `finance/profit_policy.py` | `test_finance.py::test_balanced_split`, `test_rounding_to_savings` | |
| Protected savings never fund trades | 68 | `finance/accounts.py` | `test_finance.py::test_protected_excluded` | |
| Ledger schema & append-only audit | 76 | `db/schema.py` | `test_db.py` | |

## §12 Research AI (optional Claude) (p.19, 45, 93–95)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Disabled by default; core runs without it | 19, 45, 99 | `research/analyst.py` | `test_research.py::test_disabled` | |
| $10 cap, $8 cutoff, $2 reserve, reservation ledger, unknown price → fail closed | 19, 94 | `research/budget.py` | `test_research.py::test_budget_*` | |
| JSON schema validation; hypotheses lifecycle | 19, 95 | `research/hypotheses.py` | `test_research.py::test_malicious_output` | |

## §13 Deployment & operations (p.18, 37, 41, 113–115, 139, 143)

| Req | Page | Artifact | Status |
|---|---|---|---|
| Inspect VPS CPU/RAM/disk/services/DNS before deploy; don't overwrite | 113, 139, 143 | `deploy/inspect_vps.sh` | |
| Non-root service account, venv, systemd Restart=on-failure, resource limits | 18, 113 | `deploy/systemd/*.service` | |
| Caddy HTTPS, ACME auto-renew, same-origin API/WS | 115, 141 | `deploy/Caddyfile` | |
| Firewall, SSH keys, unattended upgrades, chrony | 18, 49 | `deploy/provision.sh` | |
| Encrypted off-host backups, restore drill | 18, 41 | `deploy/backup.sh`, `RUNBOOK.md` | |
| Restart in halted state until reconciliation | 18 | `engine/core.py` startup | |

## §14 Interface (p.116–122, 126–138, 142–143)

| Req | Page | Artifact | Status |
|---|---|---|---|
| Exactly three primary tabs: Markets, Trade, Community | 129, 137, 138 | `frontend/src/App.tsx` | |
| Palette tokens (V11) | 117, 131 | `frontend/tailwind.config.ts` | |
| Desktop: header (logo/tabs/connection/settings), KPI strip, chart ~65%, decision+book 35%, bottom positions/countdown/activity, status bar | 132 | `frontend/src/views/Markets.tsx` | |
| No page scroll at 1366×768/1440p/1920×1080/2560×1440 | 132, 138, 143 | Playwright check | |
| Chart default Kalshi YES cents; NO series; BTC reference ($) w/ strike, S/R, volume; no mixed units; source + timestamp | 132 | `frontend/src/components/MarketChart.tsx` | |
| Decision panel: probability, confidence separate, net edge after fees, action, reason codes, expandable registry/missed/diagnostics | 132 | `DecisionPanel.tsx` | |
| Trade: ticket w/ fees, max loss, payout, review/confirm; iPhone bottom sheet | 135 | `views/Trade.tsx` | |
| Community: opt-in leaderboard, Today/7d/30d/All, live/paper, empty state | 136 | `views/Community.tsx` | |
| Animations 100–250 ms, interruptible, reduced motion; no fake live | 134 | `components/AnimatedNumber.tsx` | |
| PWA manifest, 192/512/apple icons, standalone, safe areas, 44 px targets, SW static-only | 116, 137, 142 | `frontend/public/*`, `sw.js` | |
| Push notifications (optional, VAPID, no paid provider) | 116, 142 | `api/push.py`, `sw.js` | |
| Stale/offline labels, server-synced countdown, reconnect w/ snapshot | 116, 134 | `frontend/src/state/stream.ts` | |
| Funds drawer: active/reserved/protected/reserve | 120 | `drawers/Funds.tsx` | |
| Research drawer: calibration, samples, edge decay, skipped, replay, Claude budget | 120 | `drawers/Research.tsx` | |
| Settings: mode, pause, immutable limits, notifications, security; re-auth for sensitive changes | 121 | `drawers/Settings.tsx` | |

## §15 Multi-user & community (p.129, 136–138)

| Req | Page | Module | Test | Status |
|---|---|---|---|---|
| Tenant isolation: creds, config, risk, ledger, orders | 137 | `api/deps.py`, all queries tenant-scoped | `test_api.py::test_cross_tenant_denied` | |
| Leaderboard metrics: realized net after fees, cash-flow-adjusted %, W/L/BE, closed episodes, UTC day | 136 | `community/leaderboard.py` | `test_community.py` | |
| Opt-in default private, revocation, paper/live separated, no fake users | 136 | same | `test_community.py::test_opt_in_*` | |
| Manual vs bot origin; shared buying power; bot doesn't manage manual unless enabled | 135 | `engine/manual.py` | `test_engine.py::test_manual_and_bot_share_funds` | |

## §16 Open questions / unresolved (must be answered on the VPS or by the operator)

1. Exact current Kalshi field names (`*_dollars`, `*_fp`) and the V2 order path (`/portfolio/events/orders`
   vs legacy `/portfolio/orders`) — parser accepts both; `kairos verify-api` records which is live.
2. Whether `KXBTC15M` "price to beat" is the open-minute BRTI average exposed as `floor_strike` (or similar) —
   parsed from market metadata; if absent the market is not tradable (fail closed).
3. Crypto-series fee multiplier / maker fees — read from `GET /series/{ticker}` (`fee_type`,
   `fee_multiplier`); unknown → trading disabled (p.14).
4. CF Benchmarks BRTI entitlement: not licensed → proxy only, uncertainty tracked.
5. Legal eligibility of other users before multi-user rollout (p.137) — operator decision.
