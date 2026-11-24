# Kairos V11.2

Paper-first, mathematics-first research and execution system for Kalshi BTC 15-minute contracts, with a
private browser terminal and an installable iPhone PWA (Markets · Trade · Community).

Built from scratch from `Kairos_Master_Specification_v11_2.pdf` and `Kairos_Design_Assets_v11.zip` only.

> **Status: PAPER MODE. Live trading is disabled** and can only be enabled administratively after the
> validation gates in `docs/VALIDATION_REPORT.md` pass. No profit is guaranteed; fees, minimum sizes and
> infrastructure costs can make trading uneconomic. Use only disposable funds.

## Layout

```
kairos/
  backend/            Python 3.12+ · FastAPI · NumPy/SciPy · scikit-learn/LightGBM · SQLAlchemy(PostgreSQL) · Parquet
    src/kairos/       config, money/fees, kalshi/, feeds/, market/, integrity/, features/, levels/, models/,
                      strategy/, risk/, execution/, ledger/, finance/, storage/, replay/, research/, security/,
                      db/, engine/, api/, community/, cli.py
    tests/            unit/ integration/ faults/ harness/ (offline layout-test harness, never deployed)
  frontend/           React 18 · TypeScript · Vite · Tailwind · Lightweight Charts · Motion · Lucide · supplied SVG icons
  deploy/             inspect_vps.sh, provision.sh, install.sh, rollback.sh, backup.sh, restore_drill.sh, Caddyfile, systemd/
  docs/               ROADMAP, TRACEABILITY, API_VERIFICATION, ENGINEERING (formulas, strategy/model cards, data
                      dictionary), RUNBOOK, VALIDATION_REPORT, screenshots/
```

## Quick start (development)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e "backend[test]"
cd backend && pytest -q                      # 114 tests
cd ../frontend && npm ci && npm run build
# real engine against live Kalshi/Coinbase/Kraken (needs outbound network):
KAIROS_DATABASE_URL=sqlite:///data/dev.db kairos create-user owner
kairos serve --dev --static ../frontend/dist   # http://127.0.0.1:8000
```

Without exchange network access, `python backend/tests/harness/fixture_server.py --static frontend/dist`
runs the real engine against a synthetic fixture with a permanent "LAYOUT TEST — SYNTHETIC FIXTURE DATA"
banner (used only for the screenshots in `docs/screenshots/`).

## Production

See `docs/RUNBOOK.md`: inspect the IONOS VPS → provision → install → `kairos verify-api` →
`kairos accept-rules` → create owner account → paper trading. Live remains administrative.
