#!/usr/bin/env bash
# Remove old /opt/govscout/releases/<commit> directories, keeping /opt/govscout/current's
# target plus the 2 most-recently-built releases (for quick rollback). Run as root:
# sudo bash prune-releases.sh

set -euo pipefail

RELEASES_DIR=/opt/govscout/releases
KEEP=3

current=$(readlink -f /opt/govscout/current)
current_name=$(basename "$current")

# Sort by mtime, newest first.
mapfile -t all_releases < <(ls -t "$RELEASES_DIR")

keep_set=("$current_name")
for release in "${all_releases[@]}"; do
    if [ "${#keep_set[@]}" -ge "$KEEP" ]; then
        break
    fi
    already_kept=0
    for kept in "${keep_set[@]}"; do
        [ "$kept" = "$release" ] && already_kept=1 && break
    done
    if [ "$already_kept" -eq 0 ]; then
        keep_set+=("$release")
    fi
done

echo "Keeping (current + $((KEEP - 1)) most recent):"
printf '  %s\n' "${keep_set[@]}"
echo
echo "Removing:"
removed_any=0
for release in "${all_releases[@]}"; do
    keep=0
    for kept in "${keep_set[@]}"; do
        [ "$kept" = "$release" ] && keep=1 && break
    done
    if [ "$keep" -eq 0 ]; then
        echo "  $release"
        rm -rf "${RELEASES_DIR:?}/${release:?}"
        removed_any=1
    fi
done

if [ "$removed_any" -eq 0 ]; then
    echo "  (nothing to remove)"
fi

echo
du -sh "$RELEASES_DIR"
