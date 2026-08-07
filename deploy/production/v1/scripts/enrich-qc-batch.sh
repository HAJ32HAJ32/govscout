#!/usr/bin/env bash
set -Eeuo pipefail

DB=/var/lib/govscout/govscout.sqlite3
VENV=/opt/govscout/current/.venv

IDS="$(sudo -u govscout GOVSCOUT_DATABASE="${DB}" "${VENV}/bin/python3" -c "
import sqlite3
conn = sqlite3.connect('${DB}')
rows = conn.execute('''
    SELECT f.id, f.frn, f.firm_name FROM fca_firms f
    LEFT JOIN enrichment_runs e ON e.firm_id = f.id
    WHERE e.id IS NULL
    ORDER BY f.id
''').fetchall()
for r in rows:
    print(f'{r[0]}\t{r[1]}\t{r[2]}')
")"

if [[ -z "${IDS}" ]]; then
    echo "No unscored firms found — everything already has an enrichment run."
    exit 0
fi

echo "Firms needing enrichment + QC:"
echo "${IDS}"
echo

while IFS=$'\t' read -r firm_id frn name; do
    [[ -z "${firm_id}" ]] && continue
    echo "=== Firm ${firm_id} (FRN ${frn}, ${name}) ==="
    sudo -u govscout GOVSCOUT_DATABASE="${DB}" "${VENV}/bin/govscout" enrich-fca "${firm_id}" || true
    sudo -u govscout GOVSCOUT_DATABASE="${DB}" "${VENV}/bin/govscout" qc-fca "${firm_id}" || true
    echo
done <<< "${IDS}"
