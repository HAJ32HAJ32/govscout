#!/usr/bin/env bash
# One-shot setup: private, loopback-only SearXNG instance behind Caddy (internal TLS CA),
# wired into GovScout via GOVSCOUT_SEARCH_ENDPOINT. Fully automated end to end -- run this
# once as root: sudo bash setup_searxng.sh
#
# What it does, in order:
#   1. Installs Docker (docker.io) if not already present.
#   2. Runs a SearXNG container bound to 127.0.0.1:8080 only (never reachable from the internet).
#   3. Enables JSON output format in its settings.yml (disabled by default, required by GovScout).
#   4. Adds an /etc/hosts entry so this box resolves searxng.govscout.internal -> 127.0.0.1.
#   5. Adds a Caddy site block for that hostname on the SAME shared :443 listener as the
#      existing public site (a second exclusive bind on 127.0.0.1:443 conflicts with the
#      existing wildcard listener -- that was the earlier bug), but rejects any connection
#      whose real source IP isn't loopback, so it's still unreachable from the internet even
#      though the TLS port is shared. Uses Caddy's internal CA (`tls internal`), so nothing
#      here touches real DNS or the public internet.
#   6. Installs Caddy's internal root CA into the system trust store so GovScout's Python
#      process (plain urllib, default cert verification) will trust the HTTPS connection.
#   7. Smoke-tests the endpoint over HTTPS.
#   8. If the smoke test passes, adds GOVSCOUT_SEARCH_ENDPOINT to /etc/govscout/govscout.env
#      and restarts govscout.service.
#
# Safe to re-run: every step is idempotent (backs up files it edits, replaces its own
# previously-added blocks rather than duplicating them).

set -uo pipefail

HOSTNAME="searxng.govscout.internal"

echo "== 1. Docker =="
if ! command -v docker >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y docker.io
fi
systemctl enable --now docker

echo "== 2. SearXNG container =="
mkdir -p /opt/searxng
if [ ! -f /opt/searxng/settings.yml ]; then
    docker rm -f searxng-seed >/dev/null 2>&1 || true
    docker run -d --name searxng-seed -v /opt/searxng:/etc/searxng searxng/searxng:latest >/dev/null
    for i in $(seq 1 30); do
        [ -f /opt/searxng/settings.yml ] && break
        sleep 1
    done
    docker rm -f searxng-seed >/dev/null 2>&1 || true
fi
if [ ! -f /opt/searxng/settings.yml ]; then
    echo "settings.yml never appeared -- inspect /opt/searxng manually" >&2
    exit 1
fi

echo "== 3. Enable JSON format, disable bot-detection limiter, randomise secret key =="
python3 <<'PYEOF'
import re, secrets, pathlib
p = pathlib.Path("/opt/searxng/settings.yml")
text = p.read_text()

# The docker image ships a minimal settings.yml (use_default_settings: true, no explicit
# `search:` section at all) -- there is no `formats:` key to find-and-extend here, so it
# must be added as a whole new section. SearXNG 403s any format not explicitly listed.
search_match = re.search(r"^search:[ \t]*\n((?:[ \t]+.*\n?)*)", text, re.MULTILINE)
if search_match:
    block = search_match.group(0)
    if "- json" not in block:
        if re.search(r"^\s*formats:\s*\n", block, re.MULTILINE):
            new_block = re.sub(
                r"(^\s*formats:\s*\n(?:\s*-\s*\S+\n)*)",
                lambda m: m.group(1) + "    - json\n",
                block, count=1, flags=re.MULTILINE,
            )
        else:
            new_block = block.rstrip("\n") + "\n  formats:\n    - html\n    - json\n"
        text = text[:search_match.start()] + new_block + text[search_match.end():]
else:
    text = text.rstrip("\n") + "\n\nsearch:\n  formats:\n    - html\n    - json\n"

# SearXNG's default "limiter" bot-detection blocks non-browser requests (including our own
# curl smoke test and GovScout's urllib calls) with a 403. This instance is private and only
# ever called by GovScout, so disable it outright rather than fighting header heuristics.
if re.search(r"^\s*limiter:\s*.*$", text, re.MULTILINE):
    text = re.sub(r"^(\s*)limiter:\s*.*$", r"\1limiter: false", text, flags=re.MULTILINE)
else:
    text = re.sub(r"^(server:\s*\n)", r"\1  limiter: false\n", text, flags=re.MULTILINE, count=1)

key = secrets.token_hex(32)
text = re.sub(r'secret_key:\s*".*?"', f'secret_key: "{key}"', text)
text = re.sub(r"secret_key:\s*ultrasecretkey", f'secret_key: "{key}"', text)
p.write_text(text)
print("settings.yml updated")
PYEOF

docker rm -f searxng >/dev/null 2>&1 || true
docker run -d --name searxng --restart unless-stopped \
    -p 127.0.0.1:8080:8080 \
    -v /opt/searxng:/etc/searxng \
    searxng/searxng:latest

echo "== 4. /etc/hosts entry =="
grep -q "$HOSTNAME" /etc/hosts || echo "127.0.0.1 $HOSTNAME" >> /etc/hosts

echo "== 5. Caddy site block =="
CADDYFILE=/etc/caddy/Caddyfile
cp "$CADDYFILE" "$CADDYFILE.bak.$(date +%s)"
python3 <<PYEOF
import re, pathlib
hostname = "$HOSTNAME"
p = pathlib.Path("$CADDYFILE")
text = p.read_text()
block_re = re.compile(r"\n?" + re.escape(hostname) + r" \{.*?\n\}\n?", re.DOTALL)
text = block_re.sub("", text)
new_block = f"""
{hostname} {{
	@not_local not remote_ip 127.0.0.1 ::1
	respond @not_local 403
	tls internal
	reverse_proxy 127.0.0.1:8080
}}
"""
text = text.rstrip("\n") + "\n" + new_block
p.write_text(text)
print("Caddyfile updated (previous block, if any, removed and replaced)")
PYEOF

echo "Validating Caddy config..."
if ! caddy validate --config "$CADDYFILE" --adapter caddyfile; then
    echo "Caddyfile is invalid -- not touching the running service. Fix $CADDYFILE and re-run." >&2
    exit 1
fi

echo "Restarting caddy (a clean restart, not reload, to clear any stuck previous reload attempt)..."
pkill -f "systemctl reload caddy" >/dev/null 2>&1 || true
if ! timeout 30 systemctl restart caddy; then
    echo "caddy restart timed out or failed -- check: systemctl status caddy" >&2
    exit 1
fi
sleep 1
if ! systemctl is-active --quiet caddy; then
    echo "caddy is not active after restart -- check: systemctl status caddy / journalctl -u caddy" >&2
    exit 1
fi
echo "caddy restarted cleanly."

echo "== 6. Trust Caddy's internal CA system-wide =="
# Force Caddy to mint its local CA now if it hasn't already (happens lazily on first HTTPS use).
curl -sk -o /dev/null "https://$HOSTNAME/search?q=test&format=json" || true
sleep 2
ROOT_CA=$(find /var/lib/caddy -iname "root.crt" 2>/dev/null | head -n1 || true)
if [ -z "$ROOT_CA" ]; then
    echo "Could not find Caddy's local root CA -- HTTPS trust won't work yet. Re-run this script once more." >&2
else
    cp "$ROOT_CA" /usr/local/share/ca-certificates/caddy-local-ca.crt
    update-ca-certificates
fi

echo "== 7. Smoke test =="
sleep 2
SMOKE_CODE=$(curl -s -o /tmp/searxng_test.json -w "%{http_code}" "https://$HOSTNAME/search?q=example&format=json")
echo "HTTP $SMOKE_CODE"
head -c 300 /tmp/searxng_test.json; echo

if [ "$SMOKE_CODE" != "200" ]; then
    echo
    echo "Smoke test did not return 200 -- stopping here without touching GovScout's config."
    echo "Re-run this script again (it's safe to repeat), or share this output."
    exit 1
fi

echo "== 8. Wire into GovScout and restart it =="
ENV_FILE=/etc/govscout/govscout.env
cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
grep -v '^GOVSCOUT_SEARCH_ENDPOINT=' "$ENV_FILE" > "$ENV_FILE.tmp"
echo "GOVSCOUT_SEARCH_ENDPOINT=https://$HOSTNAME/search" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
chown root:root "$ENV_FILE"
chmod 0600 "$ENV_FILE"

if ! timeout 30 systemctl restart govscout.service; then
    echo "govscout.service restart timed out or failed -- check: systemctl status govscout.service" >&2
    exit 1
fi
sleep 2
if systemctl is-active --quiet govscout.service; then
    echo
    echo "Done. GOVSCOUT_SEARCH_ENDPOINT is set and govscout.service is running with it."
    echo "Next: go add a firm with no website on /today and confirm candidate suggestions appear."
else
    echo "govscout.service is not active after restart -- check: systemctl status govscout.service" >&2
    exit 1
fi
