# Deploy incidents

Dated log of production deploy problems and their resolution. Kept separate from `deploy/production/v1/RUNBOOK.md` (the from-scratch install procedure) because these are about the *routine* redeploy path drifting from `main`, not the install itself.

## 2026-08-08: production stuck ~24h behind `main`

**What happened.** Two commits landed on `main` and were pushed to `origin` — the `/today` structural redesign (`d5b6289`) and the visual pass (`70a586e`) — but `https://leads.misegroup.co.uk` kept serving the release built from `1569828`, the commit immediately before that work started. The live page still showed the pre-redesign UI: raw check-code arrays, "Medium priority" labels, "Checks need attention" text.

**Root cause.** Deploys are not automatic on `git push`. `/opt/govscout/current` is a symlink to an immutable, `pip`-installed release under `/opt/govscout/releases/<commit-sha>/`, built from a `git archive` snapshot — moving it forward requires an explicit, separate step (`sudo bash deploy/production/v1/scripts/deploy.sh`). That step was never re-run after the two commits were merged, so `govscout.service` simply kept running the release built ~10 seconds before the redesign work began (confirmed via the release's `RELEASE` marker file and `systemctl show -p ActiveEnterTimestamp`).

**How it was found**, for reference — no live-page content check was needed; the deployed template on disk was read directly and compared against the source repo:

```console
readlink -f /opt/govscout/current
cat /opt/govscout/current/RELEASE                      # commit= told us exactly what was live
git -C /home/harrison/govscout log -1 --oneline          # compared against repo HEAD
grep -c "Medium priority\|Checks need attention" \
  /opt/govscout/current/.venv/lib/python3.12/site-packages/govscout/web/templates/today.html
systemctl show govscout.service -p ActiveEnterTimestamp
```

**Resolution — verified working end-to-end 2026-08-08.** Ran the existing, already-committed `deploy.sh` as-is (no migrations were pending — `diff`ing the repo's `resources/migrations/` against the deployed release's showed an identical `001`–`017` listing, so this was a pure code-only release exactly matching what the script is designed for):

```console
sudo install -d -o root -g root -m 0700 /var/backups/govscout
sudo cp --preserve=mode,ownership,timestamps /var/lib/govscout/govscout.sqlite3 \
  "/var/backups/govscout/govscout.sqlite3.$(date -u +%Y%m%dT%H%M%SZ)"   # optional, zero-cost insurance
sudo bash /home/harrison/govscout/deploy/production/v1/scripts/deploy.sh
```

Post-deploy verification (no login credentials needed — reads the newly-built release straight off disk, which is exactly what Gunicorn renders):

```console
NEW=$(readlink -f /opt/govscout/current)
grep -c "Medium priority\|Checks need attention" "$NEW"/.venv/lib/python3.12/site-packages/govscout/web/templates/today.html   # 0
grep -o "Ready to review\|Not yet researched\|Hot ·\|Warm ·\|Cool ·" "$NEW"/.venv/lib/python3.12/site-packages/govscout/web/templates/*.html | sort -u
cat "$NEW"/RELEASE                                                     # commit= matches `git rev-parse HEAD`
systemctl show govscout.service -p ActiveEnterTimestamp                # moved past the deploy time
curl -s -o /dev/null -w "%{http_code}\n" https://leads.misegroup.co.uk/login   # 200
```

All checks passed against commit `9db5ad0`.

**Prevention.** `/today` now has a quiet footer ("Deployed `<commit>`") in its bottom-left corner, read once at application startup from the release's `RELEASE` marker (falling back to `git rev-parse` in local dev, and to the literal string `unknown` if neither is available — see `src/govscout/web/deploy_info.py`). This makes "what's actually live" checkable at a glance from the page itself, without needing to SSH in and re-run this whole diagnosis. Still true that deploys remain a manual step — the footer doesn't fix the underlying manual-deploy gap, it just makes drift immediately visible instead of silent.
