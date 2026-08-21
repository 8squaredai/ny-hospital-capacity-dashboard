#!/usr/bin/env bash
# Refreshes data/New_York_State_Statewide_Hospital_Bed_Capacity.csv from NY DOH's
# live dataset and, if it changed, commits (and optionally pushes) the update.
# Meant to run once a day -- see .github/workflows/update-data.yml for the
# scheduled version. Safe to run by hand too.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SOURCE_URL="https://health.data.ny.gov/api/views/2dbc-sqe7/rows.csv?accessType=DOWNLOAD"
DEST="data/New_York_State_Statewide_Hospital_Bed_Capacity.csv"
TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

PUSH=0
for arg in "$@"; do
    [ "$arg" = "--push" ] && PUSH=1
done

echo "Downloading latest hospital bed capacity data..."
curl -sS --fail --retry 3 --retry-delay 5 -o "$TMP_FILE" "$SOURCE_URL"

# Guard against a truncated/HTML-error response silently overwriting good data.
if [ ! -s "$TMP_FILE" ]; then
    echo "Download failed: empty response." >&2
    exit 1
fi
if ! head -1 "$TMP_FILE" | grep -q "Facility PFI"; then
    echo "Download failed: response doesn't look like the expected CSV (no 'Facility PFI' header)." >&2
    exit 1
fi

new_rows=$(($(wc -l < "$TMP_FILE") - 1))
if [ -f "$DEST" ]; then
    old_rows=$(($(wc -l < "$DEST") - 1))
    # The dataset only grows (new daily reporting rows appended); a sharp drop
    # means a bad download, not real data -- refuse to overwrite on that.
    min_acceptable=$((old_rows * 90 / 100))
    if [ "$new_rows" -lt "$min_acceptable" ]; then
        echo "Download failed: row count dropped from $old_rows to $new_rows (>10% smaller) -- refusing to overwrite." >&2
        exit 1
    fi
fi

if [ -f "$DEST" ] && cmp -s "$TMP_FILE" "$DEST"; then
    echo "No change: data is already up to date ($new_rows rows)."
    exit 0
fi

mv "$TMP_FILE" "$DEST"
trap - EXIT
echo "Updated $DEST ($new_rows rows)."

if git -C "$REPO_ROOT" diff --quiet -- "$DEST"; then
    echo "File updated on disk but git sees no diff (unexpected) -- nothing to commit."
    exit 0
fi

git -C "$REPO_ROOT" add "$DEST"
git -C "$REPO_ROOT" commit -m "Update hospital bed capacity data ($(date -u +%Y-%m-%d))"

if [ "$PUSH" -eq 1 ]; then
    git -C "$REPO_ROOT" push
    echo "Pushed."
else
    echo "Committed locally. Re-run with --push to push, or push manually."
fi
