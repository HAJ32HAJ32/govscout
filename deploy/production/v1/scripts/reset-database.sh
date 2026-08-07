#!/usr/bin/env bash
# Reset the GovScout production database to a clean slate for a fresh end-to-end test run.
# Backs up the current database first (to /var/backups/govscout/), then removes the live
# database file so govscout.service recreates it fresh (via wsgi.py's migrate() call) on
# restart. Run once as root: sudo bash reset_govscout_db.sh

set -euo pipefail

DB=/var/lib/govscout/govscout.sqlite3
BACKUP_DIR=/var/backups/govscout

if [ ! -f "$DB" ]; then
    echo "No existing database at $DB -- nothing to back up, will just start fresh." >&2
else
    install -d -o root -g root -m 0700 "$BACKUP_DIR"
    TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
    BACKUP="$BACKUP_DIR/govscout.sqlite3.pre-reset-${TIMESTAMP}"
    echo "== Backing up current database to $BACKUP =="
    python3 - "$DB" "$BACKUP" <<'PY'
import sqlite3, sys
from pathlib import Path
source_path, backup_path = map(Path, sys.argv[1:])
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
backup = sqlite3.connect(backup_path)
try:
    source.backup(backup)
    backup.commit()
    integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit(f"backup integrity check failed: {integrity}")
    print("backup_integrity=ok")
finally:
    source.close()
    backup.close()
PY
    chown root:root "$BACKUP"
    chmod 0600 "$BACKUP"
    sha256sum "$BACKUP"
fi

echo "== Stopping govscout.service and the processing timer =="
systemctl stop govscout-processing.timer govscout.service

echo "== Removing the live database =="
rm -f "$DB" "$DB-wal" "$DB-shm"

echo "== Restarting govscout.service (recreates and migrates a fresh database) =="
systemctl start govscout.service
sleep 2
if ! systemctl is-active --quiet govscout.service; then
    echo "govscout.service is not active after restart -- check: systemctl status govscout.service" >&2
    exit 1
fi

echo "== Restarting the processing timer =="
systemctl start govscout-processing.timer

echo "== Smoke test =="
code=$(curl -s -o /dev/null -w "%{http_code}" https://leads.misegroup.co.uk/login)
echo "login page HTTP ${code}"
if [ "$code" != "200" ]; then
    echo "Unexpected status from /login -- investigate before relying on this." >&2
    exit 1
fi

echo
echo "Database reset. Fresh dry run is ready -- import a firm via the Collector to start."
