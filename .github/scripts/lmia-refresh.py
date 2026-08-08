#!/usr/bin/env python3
"""
LMIA Quarterly Refresh — downloads ESDC Positive LMIA Employers List,
extracts unique employer names, outputs lmia-sponsors.json.

Mirrors lca-quarterly-refresh.py for Canada's LMIA data (analogous to US DOL LCA).

Data source: https://open.canada.ca/data/en/dataset/90fed587-1364-4f33-a9ee-208181dc0b97
Licence: Open Government Licence — Canada

Usage:
  python3 lmia-quarterly-refresh.py [--quarters N] [--output PATH]

  --quarters N   Number of recent quarters to include (default: 8 = 2 years)
  --output PATH  Output file path (default: lmia-sponsors.json)
"""

import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

# ESDC open data resource IDs for English XLSX files (by quarter)
# Format: (year, quarter, resource_id)
LMIA_RESOURCES = [
    (2026, 1, "4ee7a4e0-ffc3-47af-94e7-30929d1eeb67"),
    (2025, 4, "06512c1b-d4a7-413f-bc1e-fc38de9f9aca"),
    (2025, 3, "3b7a821b-becc-4d96-9e39-0c2849f90c50"),
    (2025, 2, "fbfda891-5327-4e01-927e-0e0dd580d304"),
    (2025, 1, "f995383c-800e-46dd-92fa-a06ec7a9a706"),
    (2024, 4, "36d1f9d3-9906-4079-8741-c55bf539de3b"),
    (2024, 3, "78fc6fae-db4f-46c5-b3de-b0a9cd7795c6"),
    (2024, 2, "56aba012-5b4a-4628-8398-b0d433a6d08f"),
    (2024, 1, "049928ce-9e7f-480b-9983-f5fa46f612ae"),
    (2023, 4, "aa0a56e3-244e-4958-8f60-82e5b478bebe"),
    (2023, 3, "d48a4d9a-fffa-4c02-81c9-93332c52f2df"),
    (2023, 2, "aed9328a-1b42-48bd-a096-75ef06eba025"),
    (2023, 1, "65794992-cce4-4103-a1f6-ec6e3d249e90"),
]

BASE_URL = "https://open.canada.ca/data/dataset/90fed587-1364-4f33-a9ee-208181dc0b97/resource/{}/download/tfwp_{}q{}_pos_en.xlsx"


def download_xlsx(url):
    """Download XLSX file with proper User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": "ZJP-LMIA-Refresh/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def parse_xlsx_employers(data):
    """Extract employer names from XLSX data. Returns set of lowercased employer names."""
    try:
        import openpyxl
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "openpyxl", "-q"])
        import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
    ws = wb.active
    employers = set()

    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or len(row) < 3:
            continue
        employer = str(row[2]).strip() if row[2] else ""
        if not employer or len(employer) < 2:
            continue
        # Skip footnote rows
        lower = employer.lower()
        if lower.startswith(("the numbers", "the employer", "the source", "the list", "the lmia")):
            continue
        if lower[0].isdigit() and len(employer) > 60:
            continue
        employers.add(employer.lower().strip())

    wb.close()
    return employers


def normalize_employer_name(name):
    """Normalize employer name for matching (mirrors normalizeLcaName)."""
    import re
    name = name.lower().strip()
    name = re.sub(r"[.,]", "", name)
    name = name.replace("&", "and")
    name = name.replace("-", " ")
    name = re.sub(r"\s+", "", name)
    return name


def main():
    parser = argparse.ArgumentParser(description="Download + parse ESDC LMIA employer data")
    parser.add_argument("--quarters", type=int, default=8, help="Number of recent quarters (default: 8)")
    parser.add_argument("--output", default="lmia-sponsors.json", help="Output file path")
    args = parser.parse_args()

    selected = LMIA_RESOURCES[: args.quarters]
    print(f"[lmia-refresh] Downloading {len(selected)} quarters of LMIA data...")

    all_employers = set()
    quarterly_counts = []

    for year, quarter, resource_id in selected:
        url = BASE_URL.format(resource_id, year, quarter)
        label = f"{year}Q{quarter}"
        try:
            print(f"  {label}: downloading...", end=" ", flush=True)
            data = download_xlsx(url)
            employers = parse_xlsx_employers(data)
            new = employers - all_employers
            all_employers.update(employers)
            quarterly_counts.append({"quarter": label, "employers": len(employers), "new": len(new)})
            print(f"{len(employers)} employers ({len(new)} new)")
        except Exception as e:
            print(f"FAILED: {e}")
            quarterly_counts.append({"quarter": label, "employers": 0, "new": 0, "error": str(e)})

    # Build normalized employer set for matching
    normalized = {}
    for employer in sorted(all_employers):
        norm = normalize_employer_name(employer)
        if norm and len(norm) >= 2:
            normalized[norm] = employer  # keep first (original) form

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "ESDC Positive LMIA Employers List",
        "source_url": "https://open.canada.ca/data/en/dataset/90fed587-1364-4f33-a9ee-208181dc0b97",
        "licence": "Open Government Licence — Canada",
        "quarters_covered": [q["quarter"] for q in quarterly_counts],
        "quarterly_breakdown": quarterly_counts,
        "total_unique_employers": len(all_employers),
        "total_normalized": len(normalized),
        "employers": sorted(all_employers),
        "employers_normalized": sorted(normalized.keys()),
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n[lmia-refresh] Output: {args.output}")
    print(f"  Total unique employers: {len(all_employers)}")
    print(f"  Normalized forms: {len(normalized)}")
    print(f"  Quarters: {', '.join(q['quarter'] for q in quarterly_counts)}")


if __name__ == "__main__":
    main()
