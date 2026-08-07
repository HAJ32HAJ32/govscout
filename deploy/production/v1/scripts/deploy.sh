#!/usr/bin/env bash
# Build and deploy the current govscout main commit as a new release, matching the
# existing /opt/govscout/releases/<commit>/ + symlink-switch pattern already used on
# this box (see the RELEASE files in older release directories using
# built_from=git-archive:<commit>). Run once as root: sudo bash deploy_govscout.sh
#
# Does NOT touch the database or /etc/govscout/govscout.env -- this is a code-only
# release (no new migrations in this change).

set -euo pipefail

REPO=/home/harrison/govscout
COMMIT=$(git -C "$REPO" rev-parse HEAD)
RELEASE_DIR="/opt/govscout/releases/${COMMIT}"

if [ -d "$RELEASE_DIR" ]; then
    echo "Release ${COMMIT} already exists at ${RELEASE_DIR} -- nothing to build." >&2
else
    echo "== Building release ${COMMIT} from a clean git-archive checkout =="
    SRC=$(mktemp -d)
    trap 'rm -rf "$SRC"' EXIT
    git -C "$REPO" archive "$COMMIT" | tar -x -C "$SRC"

    mkdir -p "$RELEASE_DIR"
    python3 -m venv "$RELEASE_DIR/.venv"
    "$RELEASE_DIR/.venv/bin/pip" install --upgrade pip wheel >/dev/null
    "$RELEASE_DIR/.venv/bin/pip" install "$SRC"

    cat > "$RELEASE_DIR/RELEASE" <<EOF
commit=${COMMIT}
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
built_from=git-archive:${COMMIT}
EOF
    chown -R root:root "$RELEASE_DIR"
fi

echo "== Switching /opt/govscout/current -> ${COMMIT} =="
ln -sfn "$RELEASE_DIR" /opt/govscout/current

echo "== Restarting govscout.service =="
systemctl restart govscout.service
sleep 2
if ! systemctl is-active --quiet govscout.service; then
    echo "govscout.service is not active after restart -- check: systemctl status govscout.service" >&2
    exit 1
fi

echo "== Smoke test =="
code=$(curl -s -o /dev/null -w "%{http_code}" https://leads.misegroup.co.uk/login)
echo "login page HTTP ${code}"
if [ "$code" != "200" ]; then
    echo "Unexpected status from /login -- investigate before relying on this release." >&2
    exit 1
fi

echo
echo "Deployed ${COMMIT}. Also restarting the processing timer's next run will pick up"
echo "the new code automatically (it execs /opt/govscout/current/.venv/bin/govscout each tick)."
