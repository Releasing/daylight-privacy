#!/usr/bin/env bash
# Atomic rollback to the previous release (spec p.18, p.42). Ledger/database are not rolled back.
set -euo pipefail
APP_DIR=/opt/kairos-v11
PREV=$(cat $APP_DIR/previous_release)
ln -sfn "$PREV" $APP_DIR/current.new && mv -T $APP_DIR/current.new $APP_DIR/current
systemctl restart kairos-web.service
echo "Rolled back to $PREV (restarts PAUSED until reconciliation)."
