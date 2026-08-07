#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE_ID="7cb25999a3fe769f0ffdda6b09bb20724844476e"
RELEASE="/opt/govscout/releases/${RELEASE_ID}"
SOURCE_DB="/home/harrison/.local/share/govscout/govscout.sqlite3"
TARGET_DB="/var/lib/govscout/govscout.sqlite3"
ENV_FILE="/etc/govscout/govscout.env"
BACKUP_DIR="/var/backups/govscout"
USER_RUNTIME="/run/user/1000"
OLD_ACTIVE=0
UFW_HTTP_ADDED=0
UFW_HTTPS_ADDED=0
CUTOVER_COMPLETE=0

user_systemctl() {
    runuser -u harrison -- env XDG_RUNTIME_DIR="${USER_RUNTIME}" systemctl --user "$@"
}

rollback() {
    local status=$?
    if [[ ${CUTOVER_COMPLETE} -eq 1 ]]; then
        return
    fi
    echo "Cutover failed; restoring the prior private service." >&2
    systemctl disable --now govscout.service >/dev/null 2>&1 || true
    systemctl disable --now caddy.service >/dev/null 2>&1 || true
    systemctl mask caddy.service >/dev/null 2>&1 || true
    if [[ ${UFW_HTTPS_ADDED} -eq 1 ]]; then
        ufw --force delete allow 443/tcp >/dev/null 2>&1 || true
    fi
    if [[ ${UFW_HTTP_ADDED} -eq 1 ]]; then
        ufw --force delete allow 80/tcp >/dev/null 2>&1 || true
    fi
    if [[ ${OLD_ACTIVE} -eq 1 ]]; then
        user_systemctl enable govscout.service >/dev/null 2>&1 || true
        user_systemctl start govscout.service >/dev/null 2>&1 || true
    fi
    echo "Rollback attempted; inspect service status before retrying." >&2
    exit "${status}"
}
trap rollback ERR INT TERM

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this cutover with sudo." >&2
    exit 1
fi

[[ "$(readlink -f /opt/govscout/current)" == "${RELEASE}" ]] || {
    echo "Current release link is not the reviewed merge release." >&2
    exit 1
}
[[ -f "${SOURCE_DB}" ]] || { echo "Missing source database." >&2; exit 1; }
[[ -f "${ENV_FILE}" ]] || { echo "Missing production environment file." >&2; exit 1; }
[[ ! -e "${TARGET_DB}" ]] || { echo "Refusing to overwrite ${TARGET_DB}." >&2; exit 1; }
[[ "$(stat -c '%a:%U:%G' "${ENV_FILE}")" == "600:root:root" ]] || {
    echo "Production environment permissions must be 0600 root:root." >&2
    exit 1
}

systemd-analyze verify /etc/systemd/system/govscout.service
caddy validate --config /etc/caddy/Caddyfile

if user_systemctl is-active --quiet govscout.service; then
    OLD_ACTIVE=1
fi
if [[ ${OLD_ACTIVE} -ne 1 ]]; then
    echo "Expected the prior Tailscale GovScout service to be active." >&2
    exit 1
fi

echo "Stopping prior Tailscale-only service for a consistent snapshot..."
user_systemctl stop govscout.service
user_systemctl is-active --quiet govscout.service && {
    echo "Prior service did not stop." >&2
    exit 1
} || true

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${BACKUP_DIR}/govscout-pre-${RELEASE_ID}-${TIMESTAMP}.sqlite3"
install -d -o root -g root -m 0700 "${BACKUP_DIR}"
umask 077
python3 - "${SOURCE_DB}" "${BACKUP}" <<'PY'
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

install -o govscout -g govscout -m 0600 "${BACKUP}" "${TARGET_DB}"
chmod 0755 "${RELEASE}"

systemctl enable --now govscout.service
for _ in $(seq 1 40); do
    CODE="$(curl --silent --output /dev/null --write-out '%{http_code}' \
        --header 'Host: leads.misegroup.co.uk' \
        http://127.0.0.1:8766/login || true)"
    [[ "${CODE}" == "200" ]] && break
    sleep 1
done
[[ "${CODE:-}" == "200" ]] || {
    systemctl status govscout.service --no-pager >&2 || true
    journalctl -u govscout.service -n 80 --no-pager >&2 || true
    echo "GovScout did not become ready on loopback." >&2
    exit 1
}

TODAY_CODE="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --header 'Host: leads.misegroup.co.uk' http://127.0.0.1:8766/today)"
FORGED_CODE="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --header 'Host: attacker.example' http://127.0.0.1:8766/login)"
[[ "${TODAY_CODE}" == "302" ]] || { echo "Expected /today 302, got ${TODAY_CODE}." >&2; exit 1; }
[[ "${FORGED_CODE}" == "400" ]] || { echo "Expected forged Host 400, got ${FORGED_CODE}." >&2; exit 1; }

/opt/govscout/current/.venv/bin/python - "${TARGET_DB}" <<'PY'
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
if integrity != "ok" or foreign_keys or migrations != [f"{n:03d}" for n in range(1, 9)]:
    raise SystemExit("production database verification failed")
PY

LISTENER="$(ss -H -ltn '( sport = :8766 )')"
echo "${LISTENER}"
grep -q '127.0.0.1:8766' <<<"${LISTENER}" || {
    echo "Production listener is not on exact loopback." >&2
    exit 1
}
grep -q '100.72.212.14:8766' <<<"${LISTENER}" && {
    echo "Prior Tailscale listener is unexpectedly still active." >&2
    exit 1
}

user_systemctl disable govscout.service

if ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp
    UFW_HTTP_ADDED=1
    ufw allow 443/tcp
    UFW_HTTPS_ADDED=1
fi

install -d -o caddy -g caddy -m 0750 /var/log/caddy
systemctl unmask caddy.service
systemctl enable --now caddy.service

HTTPS_CODE=""
for _ in $(seq 1 90); do
    HTTPS_CODE="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        --resolve leads.misegroup.co.uk:443:127.0.0.1 \
        https://leads.misegroup.co.uk/login 2>/dev/null || true)"
    [[ "${HTTPS_CODE}" == "200" ]] && break
    sleep 2
done
if [[ "${HTTPS_CODE}" != "200" ]]; then
    systemctl status caddy.service --no-pager >&2 || true
    journalctl -u caddy.service -n 100 --no-pager >&2 || true
    echo "HTTPS did not become ready." >&2
    exit 1
fi

HEADERS="$(curl --silent --show-error --head \
    --resolve leads.misegroup.co.uk:443:127.0.0.1 \
    https://leads.misegroup.co.uk/login)"
grep -qi '^strict-transport-security: max-age=31536000' <<<"${HEADERS}"
grep -qi '^x-frame-options: DENY' <<<"${HEADERS}"

TODAY_HTTPS="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --resolve leads.misegroup.co.uk:443:127.0.0.1 \
    https://leads.misegroup.co.uk/today)"
[[ "${TODAY_HTTPS}" == "302" ]] || {
    echo "Expected HTTPS /today 302, got ${TODAY_HTTPS}." >&2
    exit 1
}

cat >"${BACKUP_DIR}/release-${RELEASE_ID}.txt" <<EOF
release=${RELEASE_ID}
source_database=${SOURCE_DB}
production_database=${TARGET_DB}
backup=${BACKUP}
backup_sha256=${BACKUP_SHA}
cutover_utc=${TIMESTAMP}
EOF
chmod 0600 "${BACKUP_DIR}/release-${RELEASE_ID}.txt"
chown root:root "${BACKUP_DIR}/release-${RELEASE_ID}.txt"

CUTOVER_COMPLETE=1
trap - ERR INT TERM

echo "govscout_system=$(systemctl is-active govscout.service)"
echo "caddy=$(systemctl is-active caddy.service)"
echo "old_user_service=$(user_systemctl is-active govscout.service 2>/dev/null || true)"
echo "https_login=${HTTPS_CODE}"
echo "https_today=${TODAY_HTTPS}"
echo "Cutover complete."
