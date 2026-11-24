#!/usr/bin/env bash
# One-time provisioning on Ubuntu 24.04 (spec p.18, p.49, p.113–115, p.139). Idempotent. Run with sudo.
# Creates a dedicated non-root service account, directories, PostgreSQL role/database and Caddy.
# Does NOT touch other applications. Review inspect_vps.sh output first.
set -euo pipefail
APP_USER=kairosv11
APP_DIR=/opt/kairos-v11
DATA_DIR=/var/lib/kairos-v11
ETC_DIR=/etc/kairos-v11

apt-get update
apt-get install -y python3 python3-venv python3-dev build-essential postgresql chrony ufw unattended-upgrades curl debian-keyring debian-archive-keyring apt-transport-https gnupg git nodejs npm
# Caddy from the official repository (free, automatic HTTPS via ACME)
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update && apt-get install -y caddy
fi
systemctl enable --now chrony unattended-upgrades

id -u $APP_USER >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/$APP_USER --shell /usr/sbin/nologin $APP_USER
install -d -o $APP_USER -g $APP_USER -m 0750 $APP_DIR $DATA_DIR $DATA_DIR/secrets
install -d -o root -g $APP_USER -m 0750 $ETC_DIR
chmod 0700 $DATA_DIR/secrets
[ -f $ETC_DIR/backup.pass ] || (umask 077 && openssl rand -base64 48 > $ETC_DIR/backup.pass)
[ -f $ETC_DIR/kairos.env ] || install -o root -g $APP_USER -m 0640 "$(dirname "$0")/kairos.env.example" $ETC_DIR/kairos.env

# PostgreSQL: separate role + database namespace; local socket peer/scram only
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='kairosv11'" | grep -q 1 || sudo -u postgres psql -c "CREATE ROLE kairosv11 LOGIN"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='kairos_v11'" | grep -q 1 || sudo -u postgres createdb -O kairosv11 kairos_v11

# Firewall: SSH + HTTPS only. Health/control endpoints are never exposed directly (uvicorn binds 127.0.0.1).
ufw allow OpenSSH
ufw allow 80/tcp   # ACME HTTP-01 + redirect only
ufw allow 443/tcp
ufw --force enable
echo "Provisioned. Next: verify SSH key login works, then set 'PasswordAuthentication no' and 'PermitRootLogin no' in /etc/ssh/sshd_config.d/ and reload sshd."
