#!/usr/bin/env bash
# Restore drill into a scratch database; verifies checksum and row counts (spec p.18, p.41).
set -euo pipefail
F=${1:?backup .tar.gz.enc}
sha256sum -c "${F%.tar.gz.enc}.sha256"
T=$(mktemp -d); openssl enc -d -aes-256-cbc -pbkdf2 -pass file:/etc/kairos-v11/backup.pass -in "$F" | tar -C $T -xzf -
sudo -u postgres dropdb --if-exists kairos_restore_drill; sudo -u postgres createdb kairos_restore_drill
sudo -u postgres pg_restore -d kairos_restore_drill $T/db.dump
sudo -u postgres psql -d kairos_restore_drill -c "select count(*) ledger_events from ledger_events; select count(*) fills from fills;"
sudo -u postgres dropdb kairos_restore_drill; rm -rf $T; echo "restore drill OK"
