#!/usr/bin/env bash
# Install / upgrade the Kairos build (spec p.18, p.143). Run with sudo from the repository's kairos/ directory.
# Frontend is built here outside peak trading load; services restart into PAUSED until reconciliation.
set -euo pipefail
APP_DIR=/opt/kairos-v11
SRC=$(cd "$(dirname "$0")/.." && pwd)
REL=$APP_DIR/releases/$(date +%Y%m%d%H%M%S)
install -d -o kairosv11 -g kairosv11 $APP_DIR/releases
cp -a "$SRC/backend" "$REL-backend.tmp" && mv "$REL-backend.tmp" "$REL" && mkdir -p "$REL/web"
( cd "$SRC/frontend" && nice -n 15 npm ci --no-audit --no-fund && nice -n 15 npm run build ) && cp -a "$SRC/frontend/dist/." "$REL/web/"
python3 -m venv "$REL/.venv"
"$REL/.venv/bin/pip" install --upgrade pip
"$REL/.venv/bin/pip" install "$REL"
chown -R kairosv11:kairosv11 "$REL"
install -d $APP_DIR/deploy && install -m 0750 "$SRC/deploy/"*.sh $APP_DIR/deploy/
PREV=$(readlink -f $APP_DIR/current || true)
ln -sfn "$REL" $APP_DIR/current.new && mv -T $APP_DIR/current.new $APP_DIR/current
[ -n "$PREV" ] && echo "$PREV" > $APP_DIR/previous_release
install -m 0644 "$SRC/deploy/systemd/"*.service "$SRC/deploy/systemd/"*.timer /etc/systemd/system/
install -m 0644 "$SRC/deploy/Caddyfile" /etc/caddy/Caddyfile.kairos
grep -q "import /etc/caddy/Caddyfile.kairos" /etc/caddy/Caddyfile 2>/dev/null || echo "import /etc/caddy/Caddyfile.kairos" >> /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now kairos-web.service kairos-research.timer kairos-storage.timer kairos-backup.timer
caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
echo "Installed $REL. Rollback: sudo bash deploy/rollback.sh"
