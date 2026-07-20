# GovScout

GovScout is Mise's review-first lead and cold-outreach application. The P1 checkpoint establishes the safety boundary: incorporated entities only, one auditable sends ledger, concurrency-safe daily capacity, Gmail drafts only, and no autonomous send path.

## Current status

P1 is deliberately **fail-closed for production drafting**. The complete copy lint suite arrives in P4; until then, live draft commands and `/today` report `LINT_NOT_READY` before capacity is reserved or Gmail is contacted.

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
govscout draft <lead-id>
govscout draft-batch
govscout send-undo <send-id>
```

Draft-adjacent commands show the same authoritative capacity counter used by the web application. In the present production configuration, drafting stays locked with `LINT_NOT_READY`.

## Local review interface

Run the P1 surface on loopback only:

```bash
govscout web
```

Then open:

```text
http://127.0.0.1:5000/today
```

Use `govscout web --port 5050` to select another local port. The command cannot bind to a public interface. `/today` displays capacity, warnings, lock state and the due worklist; POST actions are protected by session CSRF tokens and repeat policy/sendguard checks server-side.

## Packaging

Defaults and numbered SQL migrations are package resources, so installed wheels do not depend on a source checkout. Migrations run transactionally and applied versions are checksum-verified.

## Live Gmail gate

Do not add credentials or perform a mailbox action during ordinary local development. OAuth setup and one harmless live-draft verification require separate, explicit approval. Production drafting remains locked until the complete GovScout lint policy exists and passes.
