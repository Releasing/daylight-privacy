# Kairos V11.2 — Validation Report (build sandbox, 24 Nov 2026)

**LIVE GATE: NOT PASSED — live trading remains disabled.**
(The `kairos authorize-live` command refuses to proceed unless this file records the gate as passed after
real paper/shadow evidence has been collected and reviewed by the operator.)

## 1. What was verified here

| Area | Result |
|---|---|
| Automated tests | **114 passed** (`cd backend && pytest -q`): unit, integration (engine + API in-process), fault injection, review regressions |
| Independent code review | A separate review of the money-critical paths found 8 confirmed bugs (netting cash, restore truncation/ordering/peak, manual orders priced on the wrong book, cash mutated before ledger validation, reservation leaks on exceptions, local-only live cancels). All fixed with regression tests in `tests/integration/test_review_regressions.py`. Also hardened: reservations now cover the worst case the IOC limit allows, exits never act on stale books, cancel/flatten run under the account lock, lots are no longer re-sorted by origin |
| Spec worked examples | fees $0.84/$0.79, $20.84/$29.16/$8.32, $19.59/$7.82; lots $15.75/35¢/$11.25; partial exits $9 / −$10; HWM table; $10/$8/$2 split; Kelly 40/20/10 %; confidence 85.4; EV 0.12; case A rejected — all asserted |
| Safety | paper cannot reach authenticated order path; unknown fee schedule disables trading; unverified rules block entries; stale data blocks entries while exits continue; writes never blind-retried; duplicate fills ignored; restart rebuilds state and starts PAUSED; protected savings never fund orders |
| Security | MFA login, CSRF, rate limits, cross-tenant denial, WS auth + origin, no secrets in snapshot or logs, AES-GCM tenant credentials |
| Interface | Playwright on the real server (fixture harness): desktop 1366×768, 1920×1080, 2560×1440 → no page scroll, no horizontal overflow; iPhone 390/393 and 768 portrait → no horizontal overflow, no touch target < 32 px (controls sized 44 px on touch); exactly three primary tabs; screenshots in `docs/screenshots/` (all carry the fixture banner) |
| Performance (sandbox 4 vCPU) | market view incl. 3×4,000-path MC p95 25.6 ms; full decision scan p95 70.6 ms (budget 250 ms). Must be re-measured on the 2-vCPU VPS |

## 2. What could not be verified here (honest gaps)

1. **No live Kalshi / Coinbase / Kraken data.** The build sandbox's network policy blocks those hosts and
   docs.kalshi.com. API field names were checked against search-engine excerpts of the official docs only
   (`API_VERIFICATION.md`). Run `kairos verify-api` and `kairos accept-rules` on the VPS first.
2. **No paper trading results exist yet.** Every P&L figure in screenshots is synthetic fixture data. The
   recorder, feature store and settlement calibration start accumulating on the first VPS run.
3. **VPS deployment not executed** (no SSH access from this session). Scripts are syntax-checked;
   provisioning, Caddy certificate issuance, PostgreSQL integration and systemd behaviour must be run and
   observed by the operator (`RUNBOOK.md`).
4. **PostgreSQL** was not available in the sandbox; the suite ran on SQLite through the same SQLAlchemy Core
   schema. Run `pytest` on the VPS with `KAIROS_DATABASE_URL` pointing at a scratch Postgres database.
5. **Kalshi WebSocket** requires an API key; without one the engine uses 1 s REST polling (labelled in the
   status bar). Netting semantics for YES/NO in one market and the V2 order endpoint must be confirmed.
6. **CF Benchmarks BRTI** is not licensed; settlement uses a labelled exchange proxy with basis uncertainty,
   learned only as official `expiration_value`s arrive.
7. **iPhone install and push** need HTTPS on the real domain and a physical device.
8. **Live fill ingestion / exchange reconciliation loop is not built.** Live submission is therefore refused
   unconditionally in code (`live_fill_ingestion_not_implemented`), independent of any authorization. It must
   be implemented and tested against the Kalshi demo API before any live decision.
9. **Matched arbitrage** two-leg execution is research-only; scalp strategy uses a no-drift baseline and is
   expected to show non-positive conservative EV until a drift signal is validated.

## 3. Gate checklist for a future live decision (all must be true)

- [ ] Live fill ingestion + periodic exchange reconciliation implemented and tested on the demo API
- [ ] `kairos verify-api` fingerprint recorded; rules template accepted by operator
- [ ] ≥ 30 calendar days and ≥ 1,000 resolved windows of paper/shadow data (p.44)
- [ ] Strategy net expectancy lower confidence bound > 0 (day-block bootstrap, multiple-comparison adjusted)
- [ ] Calibration ECE ≤ 5 % on held-out windows; stressed cost/latency drawdown acceptable
- [ ] Zero reconciliation mismatches, zero risk-limit violations, restart/recovery drill passed
- [ ] Backup restore drill passed; VPS latency benchmark recorded
- [ ] Operator review of fees, legal eligibility and the statement that losses can exceed gains

Until every box is checked and this file's first line is changed by the operator, Kairos stays in paper mode.
