# Historical one-time scripts

Archived, not reusable — each is hardcoded to a specific release commit/state from when it ran.
Kept for the record, not as tooling. For reusable ops scripts, see `../scripts/`.

- `2026-07-29-tailscale-to-public-cutover.sh` — moved GovScout from a Tailscale-only private
  service to the current public Caddy-fronted deployment.
- `2026-07-31-fca-collector-release-deploy.sh` — deployed the FCA-first Collector release
  (commit `311070c`), superseded by every deploy since.
