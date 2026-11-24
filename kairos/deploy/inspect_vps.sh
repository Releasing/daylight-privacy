#!/usr/bin/env bash
# Read-only inspection of the existing IONOS VPS before deployment (spec p.113, p.139, p.143).
# Changes nothing. Run as any user with sudo: bash inspect_vps.sh | tee inspect-$(date +%F).txt
set -u
echo "== OS";        cat /etc/os-release | grep -E '^(NAME|VERSION)='; uname -r
echo "== CPU/RAM";   nproc; free -h
echo "== Disk";      df -h / ; lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
echo "== Clock";     timedatectl 2>/dev/null | grep -E 'synchronized|NTP|Time zone'
echo "== Listening ports (existing services — do not overwrite)"; sudo ss -ltnp | sed 's/users:.*//'
echo "== Running services"; systemctl list-units --type=service --state=running --no-pager | head -60
echo "== Web servers present"; for b in caddy nginx apache2 httpd; do command -v $b >/dev/null && echo "  $b: $(command -v $b)"; done
echo "== PostgreSQL";  command -v psql >/dev/null && psql --version || echo "  not installed"
echo "== Python";      command -v python3 && python3 --version
echo "== Firewall";    sudo ufw status verbose 2>/dev/null || echo "  ufw not present"
echo "== Users with login shells"; awk -F: '$7 ~ /(bash|sh)$/ {print "  "$1}' /etc/passwd
echo "== Existing kairos paths (must not be reused)"; ls -ld /opt/kairos* /srv/kairos* /home/kairos* 2>/dev/null || echo "  none"
echo "== DNS for usekairos.net"; getent ahosts usekairos.net | awk '{print $1}' | sort -u; echo "  this host public IP: $(curl -s4 https://api.ipify.org || echo unknown)"
echo "== Outbound reachability"; for u in https://api.elections.kalshi.com/trade-api/v2/exchange/status https://advanced-trade-ws.coinbase.com https://ws.kraken.com; do printf "  %-60s " $u; curl -s -o /dev/null -m 8 -w "%{http_code}\n" $u; done
