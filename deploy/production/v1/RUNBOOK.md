# GovScout public deployment runbook — v1

This runbook prepares `https://leads.misegroup.co.uk` as a single-user, PC-browser review surface. Caddy terminates HTTPS; Gunicorn listens only on `127.0.0.1:8766`. Do not bind Gunicorn to `0.0.0.0`, the VPS public address, a LAN address, or a Tailscale address. GovScout does not trust `X-Forwarded-*` headers.

## 1. GoDaddy DNS

In the `misegroup.co.uk` GoDaddy DNS zone, keep GoDaddy as DNS provider and create/update exactly:

| Type | Name | Value | TTL |
| --- | --- | --- | --- |
| A | `leads` | `88.208.212.58` | 600 seconds (or GoDaddy default) |

Remove conflicting `leads` A/AAAA/CNAME records. Verify from an independent resolver before requesting a certificate. DNS changes are an operator action and are not performed by repository scripts.

## 2. Release and runtime files

1. Create and fully populate an immutable release under
   `/opt/govscout/releases/<version>`. **Do not repoint**
   `/opt/govscout/current` yet; an existing installation must first stop its
   timer and services and complete the verified backup in section 4.
2. Create a Python 3.12 virtual environment and install the built wheel with its bounded runtime dependencies. Do not run from a mutable checkout.
3. Install `govscout.service` as `/etc/systemd/system/govscout.service`,
   `govscout-processing.service` and `govscout-processing.timer` under
   `/etc/systemd/system/`, and the Caddy snippet as `/etc/caddy/Caddyfile`
   (or import it from the main Caddyfile).
4. Create the service account and private state directory:

   ```console
   sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin govscout
   sudo install -d -o govscout -g govscout -m 0700 /var/lib/govscout
   sudo install -d -o root -g root -m 0700 /etc/govscout
   ```

## 3. Credentials and private environment

Copy `govscout.env.example` to `/etc/govscout/govscout.env`. Generate values interactively; never put the plaintext password in a command line, file, shell history, log, repository, or ticket.

Generate the versioned scrypt hash using a hidden prompt:

```console
/opt/govscout/current/.venv/bin/python -c 'import getpass; from govscout.auth import hash_password; print(hash_password(getpass.getpass("Password: ")))'
```

Generate a session secret:

```console
/opt/govscout/current/.venv/bin/python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(48)).decode())'
```

Insert only the username, encoded password hash, encoded session secret, and the official Companies House API key in the env file. Never place the API key in a command line, log, repository, or ticket. Then enforce:

```console
sudo chown root:root /etc/govscout/govscout.env
sudo chmod 0600 /etc/govscout/govscout.env
```

The service refuses startup unless mode is `public-proxy`, bind is loopback, the public host is exactly `leads.misegroup.co.uk`, and all auth values validate.

Login throttling deliberately uses one persistent global throttle bucket because
this is a single-operator service and proxy client-IP headers are not trusted. A
remote attacker can therefore temporarily lock out the operator after five failed
attempts. The lockout expires after 15 minutes; do not weaken or bypass it by
restarting workers or deleting throttle records.

## 4. Backup, migrate, start

Before every upgrade of an existing installation, stop unattended processing
and the application, then make a
private, timestamped database backup. On a first installation, verify that the
database path does not exist and skip this existing-installation command block;
startup creates and migrates it.

```console
sudo systemctl disable --now govscout-processing.timer
sudo systemctl stop govscout-processing.service
sudo systemctl stop govscout
sudo install -d -o root -g root -m 0700 /var/backups/govscout
sudo cp --preserve=mode,ownership,timestamps /var/lib/govscout/govscout.sqlite3 /var/backups/govscout/govscout.sqlite3.<UTC-timestamp>
sudo sha256sum /var/backups/govscout/govscout.sqlite3.<UTC-timestamp>
```

Record the path and checksum outside the VPS. Startup applies numbered migrations in one SQLite `BEGIN IMMEDIATE` transaction and checksum-verifies all prior migrations. Migration 007 adds persistent login-throttle state. Migration 008 installs FCA identity and canonical-URL guards. Migration 009 adds hashed, revocable collector devices and immutable, payload-bound imports. Migration 010 adds immutable Companies House verification attempts and binds passing QC to a successful attempt for the same FCA firm. Migration 011 adds the bounded durable FCA-processing queue. Treat the verified pre-release database backup as part of the release artefact.

Only after that backup has been created and checksum-verified, atomically
repoint the active symlink to the already-built immutable release:

```console
sudo ln -sfn /opt/govscout/releases/<version> /opt/govscout/current
```

After migration, FCA-linked leads created through the existing Companies House-verified promotion path are backfilled into the immutable verification history. A backfilled receipt counts as current only when its recorded verification time is no more than 30 days old and its FCA source hash still matches; older or changed records must run `verify-fca <firm-id>` (or `process-fca <firm-id>`) before enrichment/QC. Use `reverify-fca <firm-id>` when fresh evidence is required; each refresh appends history and requires QC to be run again. Do not create placeholder contacts—attach a genuine address separately with `promote-fca-contact` only after legal verification succeeds.

Validate and start:

```console
sudo systemd-analyze verify /etc/systemd/system/govscout.service /etc/systemd/system/govscout-processing.service /etc/systemd/system/govscout-processing.timer
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now govscout
sudo systemctl reload caddy
```

Run the application health and security probes below before enabling unattended
processing.

## 5. Health and security probes

The unauthenticated login page is the non-data health probe; all data/review/draft routes remain protected.

```console
curl --fail --silent --show-error --resolve leads.misegroup.co.uk:443:127.0.0.1 https://leads.misegroup.co.uk/login >/dev/null
curl --silent --output /dev/null --write-out '%{http_code}\n' --resolve leads.misegroup.co.uk:443:127.0.0.1 https://leads.misegroup.co.uk/today
sudo ss -ltnp | grep 127.0.0.1:8766
sudo systemctl is-active govscout caddy
```

Expected: login probe succeeds, `/today` returns `302`, and Gunicorn has no wildcard/public listener. Confirm the response has `Secure; HttpOnly; SameSite=Strict`, CSP/frame/nosniff/referrer/cache headers, and HSTS. A forged Host must return 400.

Only after every application probe passes, enable unattended processing and
verify that the timer is active and scheduled:

```console
sudo systemctl enable --now govscout-processing.timer
sudo systemctl is-active govscout-processing.timer
sudo systemctl list-timers govscout-processing.timer --no-pager
```

For a private Collector device, issue the upload-only token after the migrated release is active:

```console
sudo -u govscout GOVSCOUT_DATABASE=/var/lib/govscout/govscout.sqlite3 \
  /opt/govscout/current/.venv/bin/govscout collector-device-add --name "H Windows PC"
```

Copy the token directly into the device's secure setup screen; do not put it in the environment
file, repository, shell scripts or logs. Revoke a lost device immediately with
`collector-device-revoke <device-id>`. Collector requests are capped per device at 12 per hour
and 100 immutable imports over that credential's lifetime; retries remain payload-bound.

## 6. Rollback

1. Disable and stop `govscout-processing.timer`, then stop
   `govscout-processing.service` and GovScout.
2. Repoint `/opt/govscout/current` to the prior immutable release.
3. When reverting across migration 008, migration 010, or migration 011, always move the current
   database aside and restore the verified pre-release backup, even when no
   corruption is apparent. Pre-008 code is not approved against migration 008's
   write-enforcing triggers. Pre-010 code does not populate
   `qc_runs.company_verification_attempt_id`, so migration 010's passing-QC
   trigger will reject its writes. Pre-011 code does not manage the durable
   processing queue. Restore owner `govscout:govscout` and mode
   `0600`.
4. Start GovScout, run the probes, and inspect `journalctl -u govscout`,
   `journalctl -u govscout-processing`, and Caddy logs. Never copy an unverified
   or live-write database over the active file.
5. Enable the processing timer only after the application, database, and queue
   state have passed those checks.

The processing timer runs only the bounded `process-fca-queue` command. It may
verify Companies House identity, enrich public website evidence, and run QC. It
does not create contacts, approve firms, draft messages, or send outreach.

## 7. PC browser-only use

Use a supported, fully updated browser on H's PC only. Navigate directly to `https://leads.misegroup.co.uk`, verify the exact hostname and HTTPS lock indicator, sign in with the single operator account, and sign out when finished. Do not save the password in shared browsers, use public/shared computers, expose the site in an embedded frame, or treat phone access as supported. Drafting remains fail-closed with `LINT_NOT_READY`; the deployment does not add an email-send path.
