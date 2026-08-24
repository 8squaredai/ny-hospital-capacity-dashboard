"""Daily refresh of the NY hospital bed-capacity dataset.

Downloads the full CSV export (not the row-capped JSON API -- see PIVOTS.md
#1 and #10) and overwrites data/hospital_history.csv only after basic sanity
checks pass, so a broken or partial download never silently corrupts the
file the app reads from. Uses requests + the standard library only, so the
GitHub Actions job doesn't need pandas installed.
"""
from __future__ import annotations

import csv
import io
import os
import sys

import requests

EXPORT_URL = "https://health.data.ny.gov/api/views/2dbc-sqe7/rows.csv?accessType=DOWNLOAD"
OUTPUT_PATH = "data/hospital_history.csv"
EXPECTED_COLUMNS = {
    "As of Date",
    "Facility PFI",
    "Facility Name",
    "DOH Region",
    "Facility County",
    "Facility Network",
    "NY Forward Region",
    "Total Staffed Acute Care Beds",
    "Total Staffed Acute Care Beds Occupied",
    "Total Staffed Acute Care Beds Available",
    "Total Staffed ICU Beds",
    "Total Staffed ICU Beds Currently Occupied",
    "Total Staffed ICU Beds Currently Available",
}
MIN_EXPECTED_ROWS = 50_000  # sanity floor -- current export is ~73,500 rows and only grows


def main() -> None:
    headers = {}
    app_token = os.environ.get("SOCRATA_APP_TOKEN")
    if app_token:
        headers["X-App-Token"] = app_token

    response = requests.get(EXPORT_URL, headers=headers, timeout=120)
    response.raise_for_status()

    reader = csv.DictReader(io.StringIO(response.text))
    missing = EXPECTED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        print(f"Aborting: downloaded CSV is missing expected columns: {missing}", file=sys.stderr)
        sys.exit(1)
    fieldnames = reader.fieldnames
    rows = list(reader)

    if len(rows) < MIN_EXPECTED_ROWS:
        print(f"Aborting: downloaded CSV has only {len(rows)} rows, expected at least {MIN_EXPECTED_ROWS}.", file=sys.stderr)
        sys.exit(1)

    # Re-serialize with consistent quoting -- Socrata's export toggles between
    # quoted and unquoted fields across requests even with identical data, which
    # would otherwise make every daily commit a full-file rewrite instead of a
    # small diff of just the new day's rows.
    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"OK: wrote {len(rows)} rows to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
