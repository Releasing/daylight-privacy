# Kairos Operator Runbook

Target: existing IONOS Linux M+ (Ubuntu 24.04, 2 vCPU, 4 GB, 120 GB NVMe) and `usekairos.net` (p.113, p.129).
No additional hosting, domains, paid AI or paid push services.

## 0. Move to a dedicated repository (recommended, p.139)
The build lives in `kairos/` of the session repository. To give it its own history:
`git subtree split -P kairos -b kairos-only` then push `kairos-only` to a new empty repository you own.

## 1. Inspect the VPS (read-only)
```bash
scp -r kairos you@VPS:~/kairos-src
ssh you@VPS 'bash ~/kairos-src/deploy/inspect_vps.sh | tee ~/inspect.txt'
```
Check: free disk/RAM, ports 80/443 not owned by another app, existing services, DNS A/AAAA of
`usekairos.net` → this VPS, outbound reachability of Kalshi/Coinbase/Kraken, clock synchronized.
If another web server owns 80/443, stop here and decide how to coexist — never overwrite other apps.

## 2. DNS
At IONOS: `A usekairos.net → <VPS IPv4>` (and `AAAA` if the VPS has IPv6). `www` optional (redirects).

## 3. Provision (once)
```bash
sudo bash ~/kairos-src/deploy/provision.sh
```
Creates user `kairosv11`, `/opt/kairos-v11`, `/var/lib/kairos-v11` (data + 0700 secrets), `/etc/kairos-v11`,
PostgreSQL role `kairosv11` + database `kairos_v11`, Caddy, chrony, unattended-upgrades, UFW (SSH/80/443).
Then harden SSH (key-only, no root login) after confirming key access.

## 4. Configure
`sudo nano /etc/kairos-v11/kairos.env` — keep `KAIROS_MODE=paper`. Set `KAIROS_REGISTRY_KEY` to a random
value (`openssl rand -hex 32`). Optional market-data key for Kalshi WebSocket books: create an API key in
the official Kalshi account UI, copy the PEM to `/var/lib/kairos-v11/secrets/kalshi_md.pem`,
`chown kairosv11: … && chmod 600 …`, set `KAIROS_KALSHI_KEY_ID`. Never paste keys into chat, Git or the browser.

## 5. Install / upgrade
```bash
cd ~/kairos-src && sudo bash deploy/install.sh
```
Builds the frontend at low priority, installs a new release under `/opt/kairos-v11/releases/…`, switches the
`current` symlink atomically, installs systemd units and the Caddy site, and reloads Caddy (ACME certificate
is obtained automatically). The engine always starts PAUSED until reconciliation completes.

## 6. Verify the official API and contract rules (required before any paper entries)
```bash
sudo -u kairosv11 bash -c 'set -a; . /etc/kairos-v11/kairos.env; /opt/kairos-v11/current/.venv/bin/kairos verify-api'
sudo -u kairosv11 bash -c 'set -a; . /etc/kairos-v11/kairos.env; /opt/kairos-v11/current/.venv/bin/kairos accept-rules'
```
`verify-api` records live response shapes (`data/fixtures/verified/`), fee type/multiplier and a fingerprint.
Read the printed `rules_primary`: confirm it matches the official contract terms (averaging window, ≥ vs >,
source index). `accept-rules` asks you to type ACCEPT. Until then every window is skipped with reason
`rules_template_unverified` (fail closed, p.2).

## 7. Create the owner account
```bash
sudo -u kairosv11 bash -c 'set -a; . /etc/kairos-v11/kairos.env; /opt/kairos-v11/current/.venv/bin/kairos create-user <name> --paper-equity 100'
```
Scan the printed `otpauth://` URI into an authenticator app (shown once). Open https://usekairos.net,
sign in. On iPhone: Safari → Share → Add to Home Screen.

## 8. Daily operation
* Markets tab: live state; Settings (gear): pause entries (exits continue), cancel pending entries,
  emergency flatten (re-auth + typed confirmation; liquidation not guaranteed), acknowledge latched halts.
* `systemctl status kairos-web`, `journalctl -u kairos-web -f`, `kairos status`.
* Timers: storage pressure hourly, shadow learning daily 04:17 UTC, encrypted backup daily 03:41 UTC.
  Set `KAIROS_BACKUP_REMOTE` for an off-host copy (local staging is not disaster recovery).
* Monthly: `sudo bash deploy/restore_drill.sh /var/lib/kairos-v11/backups/<file>.tar.gz.enc`.

## 9. Incidents (p.49, p.74)
1. Settings → Pause entries (or `systemctl stop kairos-web` — open positions then remain exposed to settlement).
2. Check halts in the status bar / Settings. Latched halts (ledger/settlement mismatch, security, write
   failure, repeated rejects, rule/fee change) need cause cleared + reconciliation + acknowledgement.
3. Suspected key exposure: revoke the key in the Kalshi UI immediately, rotate, re-store via
   `kairos set-kalshi-key`, revoke sessions (restart clears none — use a password change), preserve logs.
4. Rollback: `sudo bash deploy/rollback.sh` (database is not rolled back).

## 10. Live enablement (administrative, never automatic — p.4, p.17, p.44, p.65, p.121)
All required: ≥30 days and ≥1,000 resolved windows of paper/shadow evidence; positive conservative net
expectancy LCB; stressed-cost/latency checks; reconciliation clean; your explicit review recorded in
`docs/VALIDATION_REPORT.md` with the line `LIVE GATE: PASS`. Then:
```bash
kairos set-kalshi-key <user> --key-id <id> --key-file <pem>   # stored AES-GCM encrypted
kairos authorize-live <user> --validation-report /path/VALIDATION_REPORT.md
# set KAIROS_MODE=live in /etc/kairos-v11/kairos.env, then: sudo systemctl restart kairos-web
```
Any change to the risk policy fingerprint invalidates the authorization automatically.

## 11. Optional Claude research
Disabled by default. To enable you must set model and current per-MTok prices in `config/kairos.json`
(unknown prices fail closed); cap $10/month, cutoff $8, reserve $2. Trading never depends on it.

## 12. Push notifications (optional, free)
Run `kairos vapid-keys` once (as kairosv11); the private key stays 0600 in the secrets directory.
Users then enable push in Settings → Notifications; per-category preferences apply. iOS requires the
PWA to be installed to the Home Screen (iOS 16.4+). Halts are notified by default; other categories are opt-in.
