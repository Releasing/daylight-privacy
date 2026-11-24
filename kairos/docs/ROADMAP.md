# Kairos — Implementation Roadmap

Authoritative inputs (sole project-specific references, p.139–140):

- `Kairos_Master_Specification_v11_2.pdf` (143 pages, V1 → V11.2 addenda)
- `Kairos_Design_Assets_v11.zip` (`v11/desktop_reference.png`, `v11/palette.json`, `legacy_reference/*` icons + V9/V10 mockups)

Precedence rule applied throughout: later addenda supersede earlier pages where they conflict
(V4 p.39, V5 p.51, V6 p.66, V7 p.78, V8 p.101, V9 p.113, V10 p.123, V11 p.129, V11.1 p.139, V11.2 p.141).
The most important supersessions are listed in `TRACEABILITY.md §0`.

## Greenfield isolation record (p.139–140)

- Workspace: new directory `kairos/` created inside the repository this session was given
  (`releasing/daylight-privacy`, branch `claude/adoring-volta-qm68cd`). That repository previously held only an
  unrelated three-file privacy notice for a browser extension (`README.md`, `privacy.html`, `privacy.css`);
  no Kairos code, assets, configuration or memory existed in it and none was imported.
  **Deviation:** the spec asks for a *new empty repository*. This session cannot create GitHub repositories,
  so Kairos lives in an isolated subdirectory. Moving it to a dedicated repo is a single `git subtree split`
  (see `RUNBOOK.md`).
- No previous Kairos project, conversation, repository or memory was consulted.
- Third-party docs consulted only for verification (see `API_VERIFICATION.md`).

## Approved stack (p.141 — binding)

| Layer | Technology |
|---|---|
| Shared interface | React 18, TypeScript, Vite, Tailwind CSS |
| Charts / effects | TradingView Lightweight Charts, Motion, Lucide, custom SVG (supplied icons) |
| Backend / streaming | Python 3.12+, FastAPI, WebSockets |
| Quant | NumPy (Numba only where benchmarks justify — not yet justified) |
| Learning | scikit-learn, LightGBM — bounded background jobs |
| Persistence | PostgreSQL (transactional), Parquet/zstd (historical) |
| Deployment | Caddy (HTTPS, ACME), systemd |
| Infra | Existing IONOS VPS (2 vCPU / 4 GB / 120 GB NVMe, Ubuntu 24.04), `usekairos.net` |

Not used: Tauri, Electron, native desktop/iOS, paid AI, paid hosting, paid push provider.

## Architecture

```
Kalshi REST+WS ─┐                                  ┌─ PaperAdapter (default)
Coinbase WS ────┼─► normalizer ─► recorder(Parquet) │
Kraken WS ──────┘        │                          │
                         ▼                          │
                 market state (books, candles, final-minute benchmark proxy)
                         │
                 incremental features ─► levels/patterns
                         │
                 prediction (MC settlement, multi-horizon, first-passage scalp)
                         │
                 strategies A/B/C ─► confidence + EV + Kelly
                         │
                 independent risk gate + atomic allocator (mode controller)
                         │
                 execution (intent journal, idempotent client IDs) ─┴─ LiveAdapter (disabled; admin-gated)
                         │
                 ledger (lots, fees, high-water mark, profit buckets) ─► PostgreSQL
                         │
                 authenticated per-user snapshot+delta WebSocket ─► React (desktop / iPhone PWA)
```

Three processing lanes (p.89): fast (book, exits, risk) in the event loop; predictive (MC, sizing) in a
bounded executor; deep (training, replay) in a separate low-priority process (`kairos-research.service`,
`Nice=15`, `CPUQuota=60%`).

## Milestones (build order per p.20, p.50, p.65, p.77, p.100, p.122)

| # | Milestone | Key deliverables | Acceptance evidence |
|---|---|---|---|
| M0 | Read + plan | Roadmap, traceability, API verification log | This folder |
| M1 | Foundations | Config (fail-closed, immutable policy), Decimal money, fee adapter, Kalshi auth signer, schema-tolerant parser, REST client with token buckets/backoff, WS client, order-book builder with seq-gap resync, Coinbase/Kraken adapters, composite + feed health | `tests/unit/test_fees.py`, `test_auth.py`, `test_book.py`, `test_feeds.py` |
| M2 | Recorder + replay + ledger | Parquet recorder (UTC/venue partitions, checksums, manifest), deterministic replay, lots ledger, high-water mark, profit buckets | `test_ledger.py`, `test_finance.py`, `test_replay.py` |
| M3 | Intelligence | Incremental indicators, multi-timeframe candles, cross-asset, S/R levels + patterns, MC settlement model, multi-horizon, calibration, LR/LightGBM shadow learning + registry | `test_indicators.py`, `test_levels.py`, `test_models.py`, `test_learning.py` |
| M4 | Strategies + risk | Settlement / scalp / arbitrage, confidence, EV, Kelly, optimal exit, scanner, edge registry, risk gate, mode controller, allocator, halts, session budgets | `test_strategies.py`, `test_risk.py` |
| M5 | Execution | Intent journal + state machine, paper fill simulator (visible depth, latency, conservative queue), reconciliation, live adapter (disabled, admin-gated) | `test_execution.py`, `tests/faults/*` |
| M6 | Server | FastAPI, auth (Argon2id + TOTP MFA, HttpOnly SameSite cookies, CSRF, rate limits, audit), per-tenant engine, stream with seq/snapshot, manual Trade API, Community | `tests/integration/*` |
| M7 | Interface | React terminal (desktop) + iPhone PWA, 3 tabs only, Lightweight Charts with S/R/strike/fills, drawers, animations w/ reduced motion, manifest + static-only service worker | Playwright screenshots at 390/768/1366/1440/1920/2560 |
| M8 | Deploy kit | VPS inspection script, provisioning, Caddy, systemd (engine, research, web), Postgres, backups, restore drill, runbook, rollback | `deploy/` + `RUNBOOK.md` |
| M9 | Validation | Paper-mode run on VPS against live Kalshi data, ≥30 days / ≥1,000 resolved windows shadow before any live approval | `VALIDATION_REPORT.md` (live gate stays closed) |

## Live-trading gate (never automatic)

Live order submission requires **all** of: `KAIROS_MODE=live` in the server env file; a verified-API
fingerprint produced by `kairos verify-api` on the VPS; validation report gates passing (p.17, p.44, p.65);
explicit operator acknowledgement recorded with re-authentication (p.121); account balance/eligibility check.
Paper adapter has no code path to the authenticated order endpoint (test-enforced).

## Known technical limitations (reported, not silently replaced)

See `API_VERIFICATION.md` and `TRACEABILITY.md §Open questions`. Highlights:

1. **Official docs could not be fetched from this build sandbox** (network policy blocks docs.kalshi.com,
   api.elections.kalshi.com, kalshi.com, Coinbase and Kraken). Verification was done via search-engine
   excerpts of the official docs; every unconfirmed field is handled by a schema-tolerant parser and must be
   confirmed on the VPS with `kairos verify-api` before paper trading starts.
2. **Settlement index (CF Benchmarks BRTI) is not publicly streamed.** The engine uses a labelled composite
   Coinbase/Kraken proxy with basis uncertainty and never claims exact settlement replication (p.3, p.8, p.132).
3. **Kalshi WebSocket requires API-key authentication even for market data.** Live order books therefore need
   a (read-only usage) Kalshi API key on the server; without it the engine polls public REST at a
   rate-limited cadence and labels freshness accordingly.
4. **VPS deployment cannot be executed from this sandbox** (no SSH access). Deployment is scripted and
   documented; it must be run by the operator.
5. **PostgreSQL server unavailable in the sandbox**; the persistence layer is SQLAlchemy Core and the unit
   suite runs on SQLite, with a Postgres integration marker run on the VPS.
6. Historical tick/book data does not exist yet; recorder starts collecting on first run (p.3). No backtest
   results exist and none are claimed.
