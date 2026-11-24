#!/usr/bin/env bash
# Daily encrypted backup of ledger DB, config and model manifests (spec p.18, p.41, p.115).
# No plaintext keys. Off-host copy: set KAIROS_BACKUP_REMOTE (e.g. an rclone remote or scp target you own).
set -euo pipefail
OUT=/var/lib/kairos-v11/backups; mkdir -p $OUT; chmod 700 $OUT
TS=$(date -u +%Y%m%dT%H%M%SZ)
TMP=$(mktemp -d)
sudo -u postgres pg_dump -Fc kairos_v11 > $TMP/db.dump
cp /etc/kairos-v11/kairos.env $TMP/kairos.env.redacted && sed -i -E 's/(KEY|SECRET|PASSWORD)=.*/\1=<redacted>/' $TMP/kairos.env.redacted
cp /var/lib/kairos-v11/models/registry.json $TMP/ 2>/dev/null || true
tar -C $TMP -czf - . | openssl enc -aes-256-cbc -pbkdf2 -salt -pass file:/etc/kairos-v11/backup.pass -out $OUT/kairos-$TS.tar.gz.enc
sha256sum $OUT/kairos-$TS.tar.gz.enc > $OUT/kairos-$TS.sha256
rm -rf $TMP
find $OUT -name 'kairos-*' -mtime +14 -delete
if [ -n "${KAIROS_BACKUP_REMOTE:-}" ]; then scp -q $OUT/kairos-$TS.* "$KAIROS_BACKUP_REMOTE" && echo "copied off-host"; else echo "WARNING: no off-host target configured — local staging is not disaster recovery"; fi
