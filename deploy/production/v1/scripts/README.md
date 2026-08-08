# Operational scripts

Reusable scripts for running this deployment. All are self-contained (absolute paths, no
external args needed) and safe to re-run. Run with `sudo bash <script>` (or `sudo python3` for
`.py`) unless noted otherwise.

- `deploy.sh` — build a new release from the current `main` commit via a clean `git archive`
  checkout, switch `/opt/govscout/current`, restart `govscout.service`, smoke-test `/login`.
  Deploys are **not** automatic on `git push` — this script must be run explicitly after every
  merge to `main` that should go live. Verified working end-to-end 2026-08-08 after production
  ran ~24h behind `main` because this step was skipped; see `docs/deploy-incidents.md`.
- `reset-database.sh` — back up the live database (integrity-checked, checksummed, to
  `/var/backups/govscout/`) then remove it so `govscout.service` recreates and migrates a fresh
  empty one on restart. Destructive — confirm with H before running against real data.
- `setup-searxng.sh` — provision the private, loopback-only SearXNG instance behind Caddy that
  backs `GOVSCOUT_SEARCH_ENDPOINT` (website-candidate discovery). Idempotent; useful reference if
  that box ever needs rebuilding. See `[[GovScout]]` in the vault and the
  2026-08-06 Jarvis inbox writeup for why it's shaped this way (internal CA, source-IP-restricted,
  shares the public `:443` listener).
- `set-credentials.py` — one-time interactive setup of the `/etc/govscout/govscout.env` operator
  username/password hash. Refuses to run if credentials already exist.
- `enrich-qc-batch.sh` — find every FCA firm with no enrichment run yet and run enrichment + QC
  for each, using whatever release `/opt/govscout/current` currently points at.
- `prune-releases.sh` — remove old `/opt/govscout/releases/<commit>` directories, keeping
  `current`'s target plus the 2 most recently built (for quick rollback). Each release is a full
  venv (~20-25MB); run this occasionally rather than after every deploy.
