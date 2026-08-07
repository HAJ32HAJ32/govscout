#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE_ID="311070ce923fdb35ec8d3063c6c28d2451c086aa"
RELEASE="/opt/govscout/releases/${RELEASE_ID}"
SOURCE_REPO="/home/harrison/govscout"
TARGET_DB="/var/lib/govscout/govscout.sqlite3"
ENV_FILE="/etc/govscout/govscout.env"
BACKUP_DIR="/var/backups/govscout"
PREVIOUS_RELEASE="$(readlink -f /opt/govscout/current)"
CUTOVER_COMPLETE=0

rollback() {
    local status=$?
    if [[ ${CUTOVER_COMPLETE} -eq 1 ]]; then
        return
    fi
    echo "Upgrade failed; repointing to prior release and restarting." >&2
    ln -sfn "${PREVIOUS_RELEASE}" /opt/govscout/current
    systemctl start govscout.service >/dev/null 2>&1 || true
    echo "Rollback attempted; inspect service status and the backup under ${BACKUP_DIR} before retrying." >&2
    exit "${status}"
}
trap rollback ERR INT TERM

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this with sudo." >&2
    exit 1
fi

[[ -f "${ENV_FILE}" ]] || { echo "Missing production environment file." >&2; exit 1; }
[[ "$(stat -c '%a:%U:%G' "${ENV_FILE}")" == "600:root:root" ]] || {
    echo "Production environment permissions must be 0600 root:root." >&2
    exit 1
}
[[ -f "${TARGET_DB}" ]] || { echo "Missing production database." >&2; exit 1; }
[[ "${PREVIOUS_RELEASE}" != "${RELEASE}" ]] || { echo "This release is already current." >&2; exit 1; }

echo "Building release ${RELEASE_ID} from ${SOURCE_REPO}..."
mkdir -p "${RELEASE}"
git -C "${SOURCE_REPO}" archive "${RELEASE_ID}" | tar -x -C "${RELEASE}"
chown -R root:root "${RELEASE}"
chmod 0755 "${RELEASE}"

echo "Building virtual environment..."
python3.12 -m venv "${RELEASE}/.venv"
"${RELEASE}/.venv/bin/pip" install --upgrade pip --quiet
"${RELEASE}/.venv/bin/pip" install "${RELEASE}" --quiet

echo "Validating unit files against the new release..."
systemd-analyze verify /etc/systemd/system/govscout.service
caddy validate --config /etc/caddy/Caddyfile

echo "Stopping govscout for a consistent backup..."
systemctl stop govscout.service

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${BACKUP_DIR}/govscout-pre-${RELEASE_ID}-${TIMESTAMP}.sqlite3"
install -d -o root -g root -m 0700 "${BACKUP_DIR}"
umask 077
python3 - "${TARGET_DB}" "${BACKUP}" <<'PY'
import sqlite3
import sys
from pathlib import Path

source_path, backup_path = map(Path, sys.argv[1:])
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
backup = sqlite3.connect(backup_path)
try:
    source.backup(backup)
    backup.commit()
    for label, connection in (("source", source), ("backup", backup)):
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise SystemExit(f"{label} database verification failed")
        print(f"{label}_integrity=ok {label}_foreign_keys=0")
finally:
    backup.close()
    source.close()
PY
chown root:root "${BACKUP}"
chmod 0600 "${BACKUP}"
BACKUP_SHA="$(sha256sum "${BACKUP}" | cut -d' ' -f1)"
echo "backup=${BACKUP}"
echo "backup_sha256=${BACKUP_SHA}"

echo "Repointing /opt/govscout/current -> ${RELEASE}..."
ln -sfn "${RELEASE}" /opt/govscout/current

systemctl daemon-reload
systemctl start govscout.service

CODE=""
for _ in $(seq 1 40); do
    CODE="$(curl --silent --output /dev/null --write-out '%{http_code}' \
        --resolve leads.misegroup.co.uk:443:127.0.0.1 \
        https://leads.misegroup.co.uk/login || true)"
    [[ "${CODE}" == "200" ]] && break
    sleep 1
done
if [[ "${CODE}" != "200" ]]; then
    systemctl status govscout.service --no-pager >&2 || true
    journalctl -u govscout.service -n 80 --no-pager >&2 || true
    echo "GovScout did not become ready on the new release." >&2
    exit 1
fi

TODAY_CODE="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --resolve leads.misegroup.co.uk:443:127.0.0.1 https://leads.misegroup.co.uk/today)"
[[ "${TODAY_CODE}" == "302" ]] || { echo "Expected /today 302, got ${TODAY_CODE}." >&2; exit 1; }

"${RELEASE}/.venv/bin/python" - "${TARGET_DB}" <<'PY'
import sqlite3
import sys
p = sys.argv[1]
connection = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
try:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    migrations = [row[0] for row in connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    )]
finally:
    connection.close()
print(f"production_integrity={integrity}")
print(f"production_foreign_keys={len(foreign_keys)}")
print("production_migrations=" + ",".join(migrations))
if integrity != "ok" or foreign_keys:
    raise SystemExit("production database verification failed")
if "009" not in migrations:
    raise SystemExit("migration 009 (collector devices/imports) was not applied")
PY

systemctl reload caddy

cat >"${BACKUP_DIR}/release-${RELEASE_ID}.txt" <<EOF
release=${RELEASE_ID}
previous_release=${PREVIOUS_RELEASE}
production_database=${TARGET_DB}
backup=${BACKUP}
backup_sha256=${BACKUP_SHA}
upgraded_utc=${TIMESTAMP}
EOF
chmod 0600 "${BACKUP_DIR}/release-${RELEASE_ID}.txt"
chown root:root "${BACKUP_DIR}/release-${RELEASE_ID}.txt"

CUTOVER_COMPLETE=1
trap - ERR INT TERM

echo "release=${RELEASE_ID}"
echo "previous_release=${PREVIOUS_RELEASE}"
echo "backup=${BACKUP}"
echo "backup_sha256=${BACKUP_SHA}"
echo "govscout=$(systemctl is-active govscout.service)"
echo "caddy=$(systemctl is-active caddy.service)"
echo "https_login=${CODE}"
echo "https_today=${TODAY_CODE}"
echo "Upgrade complete. Next: issue a fresh collector device token against production with:"
echo "  sudo -u govscout GOVSCOUT_DATABASE=${TARGET_DB} /opt/govscout/current/.venv/bin/govscout collector-device-add --name \"H Windows PC\""
