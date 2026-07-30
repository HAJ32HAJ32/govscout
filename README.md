# GovScout

GovScout is Mise's review-first lead and cold-outreach application. Its active v1 beachhead is small FCA-regulated firms. FCA records are staged as discovery evidence, matched to active incorporated Companies House entities, enriched from bounded public website evidence, checked by fail-closed QC, and explicitly approved or rejected by a human. The existing outreach boundary remains intact: one auditable sends ledger, concurrency-safe daily capacity, Gmail drafts only, and no autonomous send path.

## Current status

The FCA-first discovery, enrichment, QC and review pipeline is available, while production drafting remains deliberately **fail-closed**. Until the complete copy lint suite exists, live draft commands and `/today` report `LINT_NOT_READY` before capacity is reserved or Gmail is contacted.

No Gmail OAuth credentials are required for the current checkpoint. Credential and token files must remain outside this repository.

## Safety contract

- Fixed sender: `Harrison — Mise <harrison@misegroup.co.uk>`.
- Draft creation only; no application send method, command or endpoint.
- Atomic SQLite `BEGIN IMMEDIATE` reservation before external Gmail interaction.
- Effective limits: 5 drafts on warm-up days 1–14, 8 on days 15–28, and 15 from day 29.
- Soft warning at 10; configured hard limit 15.
- First touches and follow-ups share the same daily capacity.
- UK calendar boundaries use `Europe/London`, including BST/GMT transitions.
- Exact retries are idempotent; ambiguous Gmail outcomes stay counted until reconciled.
- Undo deletes the Gmail draft before voiding—not deleting—the ledger row.
- Company eligibility is derived from Companies House profile evidence, not caller-supplied labels.
- FCA Register records enter a separate discovery-only staging table; they are not leads and cannot be drafted.
- Only an active incorporated Companies House verification receipt can promote an FCA firm into `leads`.
- Website enrichment stores source-linked evidence and honest unknown/failure states; current passing QC plus explicit human approval is required for outreach readiness.

## Development setup

Python 3.12 is the target runtime.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check .
```

The default database is created privately at:

```text
${XDG_DATA_HOME:-~/.local/share}/govscout/govscout.sqlite3
```

Set `GOVSCOUT_DATABASE` or `GOVSCOUT_CONFIG` to use explicit local paths. See `.env.example`; do not commit `.env`, OAuth credentials or tokens.

## Operator commands

```bash
govscout sends --today
govscout sends --week
govscout ingest-fca --input /path/to/fca-export.json --limit 25
govscout fca-firms --limit 25
govscout enrich-fca <firm-id>
govscout qc-fca <firm-id>
govscout collector-device-add --name "H Windows PC"
govscout collector-device-revoke <device-id>
govscout draft <lead-id>
govscout draft-batch
govscout send-undo <send-id>
```

`ingest-fca` accepts a bounded, validated JSON export of FCA Register evidence. It stages discovery records only; it does not create leads, contacts, drafts or outreach. Exact reruns are idempotent and changed records append an immutable observation rather than rewriting history. Operator-supplied exports are not live harvests; authenticated official-API acquisition is confined to the Collector described below.

## GovScout Collector private test

GovScout Collector is the Windows/macOS desktop route for the first FCA batch. It uses the
official FS Register API only—never browser scraping—and uploads no more than 25 active firms
per batch to the discovery-only staging tables. It cannot create Companies House matches,
approve firms, draft messages or send email.

Create a one-purpose upload token on the VPS:

```bash
govscout collector-device-add --name "H Windows PC"
```

The token is shown once. Enter it in Collector with the registered FCA API email/key. Collector
stores all three values in Windows Credential Manager or macOS Keychain, keeps retryable payloads
in a private local SQLite outbox, refuses redirects, and uploads only to
`https://leads.misegroup.co.uk/api/v1/collector/imports`.
The outbox refuses new collections at 25 pending batches or 25 MB, retains at most 100 batch
records, and prunes terminal records after 30 days. A server-side rejected receipt is shown as a
failed import, never as a successfully uploaded review batch.

Unsigned private-test builds are published from GitHub tags named `collector-v*`:

- `GovScout-Collector-Windows-x86_64.exe`
- `GovScout-Collector-macOS-x86_64.zip`
- `GovScout-Collector-macOS-arm64.zip`

Verify each download against its adjacent `.sha256` file. Windows may show SmartScreen because
the private-test executable is unsigned. On macOS, unzip it, then right-click the app and choose
**Open** on first launch. These builds are for H's private testing, not public distribution.

In Collector, enter one to five conservative firm-name search terms and a maximum of 25 firms.
Requests are serialized below the FCA API's published ceiling. Active results are validated,
durably queued and auto-uploaded; failed network uploads keep the same idempotency key for retry.
The exact authenticated API contract must still be checked against the FCA developer portal during
private testing; the portal documentation was not accessible from the build environment.

`enrich-fca` scans the staged firm's public HTTPS website through a bounded, no-redirect, public-address-only transport. `qc-fca` checks source freshness, site/enrichment health, duplicate websites, evidence completeness and contradictions. A record remains outreach-ineligible until QC passes and a human approves it in `/today`.

Draft-adjacent commands show the same authoritative capacity counter used by the web application. In the present production configuration, drafting stays locked with `LINT_NOT_READY`.

## Local review interface

Run the P1 surface on loopback by default:

```bash
govscout web
```

Then open:

```text
http://127.0.0.1:5000/today
```

Use `govscout web --port 5050` to select another local port. For private access from another device on the same tailnet, bind directly to the VPS's Tailscale address:

```bash
TS_IP=$(tailscale ip -4)
govscout web --host "$TS_IP" --port 8766
```

GovScout accepts only IP literals in loopback or Tailscale address ranges. Hostnames (including `localhost`), scoped IPv6 addresses, wildcard, LAN and public-IP binds are refused. Requests must also carry a strictly parsed Host header explicitly trusted for that process. `/today` displays the FCA-first evidence and review queue, current scores and QC state, capacity, warnings, lock state and the separate due worklist. Review and draft POST actions are protected by session CSRF tokens; drafting also repeats policy/sendguard checks server-side.

A hardened user-service template is provided at `deploy/govscout.service`. Before enabling it on a fresh host, create its private data directory with `install -d -m 700 ~/.local/share/govscout`. On the Mise VPS it runs continuously at `http://100.72.212.14:8766/today`, reachable only from H's tailnet. Tailscale is the access gate; GovScout does not expose this port on the public interface.

## Packaging

Defaults and numbered SQL migrations are package resources, so installed wheels do not depend on a source checkout. Migrations run transactionally and applied versions are checksum-verified.

## Production browser access

GovScout is intended to run once on the Mise VPS. H's PC is a browser client; it
does not need Python, a repository clone, or a second SQLite database. The planned
authenticated address is:

```text
https://leads.misegroup.co.uk
```

Public mode is deliberately separate from local/Tailscale development mode. It
requires the exact public hostname, a loopback-only Gunicorn bind, a validated
single-user password hash, a persistent session secret, HTTPS cookies, CSRF, and
SQLite-backed login throttling. It refuses startup when any required setting is
missing or invalid. Caddy terminates HTTPS and proxies to `127.0.0.1`; the Flask
development server is not the production runtime.

The versioned Caddy, systemd and environment templates, together with DNS,
backup, migration, health-check and rollback instructions, live in
`deploy/production/v1/RUNBOOK.md`. Never commit the populated production
environment file or a plaintext login password.

## Live Gmail gate
Do not add credentials or perform a mailbox action during ordinary local development. OAuth setup and one harmless live-draft verification require separate, explicit approval. Production drafting remains locked until the complete GovScout lint policy exists and passes.
