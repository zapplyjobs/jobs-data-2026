#!/usr/bin/env python3
"""
LCA Quarterly Data Refresh Script

Downloads quarterly LCA disclosure data from DOL OFLC, extracts certified
H-1B employer names, and produces lca-sponsors.json with a rolling N-quarter
window. REBUILDS from scratch each run (no incremental merge) so the rolling
window is always exact — no stale employers from dropped quarters.

Usage:
  python3 lca-quarterly-refresh.py --auto                    # detect latest available quarter
  python3 lca-quarterly-refresh.py --quarter FY2026_Q2       # specify latest quarter
  python3 lca-quarterly-refresh.py --dry-run                 # show what would change
  python3 lca-quarterly-refresh.py --window 4                # use 4-quarter window
Output: lca-sponsors.json — {"_meta": {...}, "employers": [...], "employer_counts": {...}}
        employers[] stays name-only (backward compatible with enrich-jobs.js loadLcaSponsors()).
        employer_counts{} (INF-LCA-COUNTS-INPUT-1): per-employer {filing_count, certified_count,
        last_certified}, keyed by the same raw strings as employers[].

After generating the file, upload to R2:
  aws s3 cp lca-sponsors.json s3://$R2_BUCKET_NAME/data/lca-sponsors.json \\
    --endpoint-url $R2_ENDPOINT

DOL source: https://www.dol.gov/agencies/eta/foreign-labor/performance
File pattern: LCA_Disclosure_Data_FY{year}_Q{n}.xlsx
"""

import argparse
import datetime
import json
import re
import sys
import tempfile
from pathlib import Path

try:
    import openpyxl
    import requests
except ImportError as e:
    print(f"Missing dependency: {e}", file=sys.stderr)
    print("Install: pip install openpyxl requests", file=sys.stderr)
    sys.exit(1)

DOL_BASE_URL = "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/{prefix}_Disclosure_Data_{quarter}.xlsx"
DEFAULT_WINDOW = 5  # quarters to keep

# Data sources: LCA (H-1B) + PERM (green card). Both filtered to Certified.
LCA_FILTER = {"CASE_STATUS": "Certified", "VISA_CLASS": "H-1B"}
PERM_FILTER = {"CASE_STATUS": "Certified"}
EMPLOYER_COL = "EMPLOYER_NAME"
PERM_EMPLOYER_COL = "EMP_BUSINESS_NAME"
SOC_COL = "SOC_CODE"  # LCA disclosure carries it; PERM does not (verified 09-07, design §6.1)
SOC_TOP = 16          # top-16 SOC prefixes kept per employer (design §6.1 cap)


def download_quarter(quarter: str, dest: Path, prefix: str = "LCA") -> bool:
    url = DOL_BASE_URL.format(prefix=prefix, quarter=quarter)
    print(f"Downloading {prefix} {quarter} from DOL...")
    try:
        resp = requests.get(url, timeout=120, stream=True)
        if resp.status_code == 404:
            print(f"  NOT FOUND (404) — {prefix} {quarter} data not yet published", file=sys.stderr)
            return False
        resp.raise_for_status()
        size_mb = int(resp.headers.get("content-length", 0)) / 1024 / 1024
        print(f"  Size: {size_mb:.1f} MB")
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
        print(f"  Downloaded to {dest}")
        return True
    except requests.RequestException as e:
        print(f"  Download failed: {e}", file=sys.stderr)
        return False


def extract_employers(xlsx_path: Path, employer_col: str = EMPLOYER_COL,
                      filters: dict = None) -> tuple[set[str], dict, dict, dict]:
    """Extract certified employer names + per-employer counts (INF-LCA-COUNTS-INPUT-1)
    + per-employer SOC prefix counts over certified rows (AGG-LCA-SOC-EXTRACTOR-MISSING-1,
    design VISA_OCCUPATION_SIGNAL_DESIGN_2026_09_07 §6.1).

    Returns (names, counts, soc_stats, soc_counts):
      names  — employer names passing `filters` (unchanged semantics; feeds employers[])
      counts — per employer name seen at all: {"filing_count", "certified_count",
               "last_certified"}. filing_count counts every row for the employer
               (any case status / visa class); certified_count only rows passing
               `filters`; last_certified = max DECISION_DATE among certified rows
               (None when the file lacks a DECISION_DATE column — verified present
               in LCA + PERM FY2026_Q1 headers, 2026-08-31).
      soc_stats    — {"certified", "with_soc"} row counters for the coverage gate
                     (zeros when the file has no SOC_CODE column — PERM).
      soc_counts   — {employer: {SOC_PREFIX: count}} over CERTIFIED rows only;
                     prefix = SOC_CODE up to the "." (e.g. "15-1252.00" → "15-1252").
                     Empty when the file has no SOC_CODE column.
    """
    if filters is None:
        filters = LCA_FILTER
    print(f"Extracting employers from {xlsx_path.name}...")
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active

    header = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    col_idx = {name: i for i, name in enumerate(header)}

    # AGG-LCA-SOC-EXTRACTOR-MISSING-1 hotfix (09-09): DOL renamed the PERM employer
    # column in fresh quarters (EMP_BUSINESS_NAME -> EMPLOYER_NAME) — try known
    # fallbacks before failing, and always return the 4-tuple callers unpack.
    if employer_col not in col_idx:
        fallback = next((c for c in ("EMPLOYER_NAME", "EMP_BUSINESS_NAME") if c in col_idx), None)
        if fallback is None:
            print(f"  ERROR: no employer column found. Columns: {header[:20]}", file=sys.stderr)
            wb.close()
            return set(), {}, {"certified": 0, "with_soc": 0}, {}
        employer_col = fallback
        print(f"  NOTE: employer column fallback -> {fallback}", file=sys.stderr)

    employer_idx = col_idx[employer_col]
    date_idx = col_idx.get("DECISION_DATE")
    if date_idx is None:
        print("  NOTE: no DECISION_DATE column — last_certified will be null for this source", file=sys.stderr)

    soc_idx = col_idx.get(SOC_COL)
    if soc_idx is None:
        print(f"  NOTE: no {SOC_COL} column — soc_counts/coverage stay empty for this source", file=sys.stderr)

    employers = set()
    counts: dict[str, dict] = {}
    total = 0
    filtered = 0
    soc_counts: dict[str, dict[str, int]] = {}
    soc_certified = 0
    soc_with = 0

    def iso_date(val) -> str | None:
        if val is None:
            return None
        if isinstance(val, datetime.datetime):
            return val.date().isoformat()
        s = str(val).strip()
        return s[:10] if len(s) >= 10 else None

    for row in ws.iter_rows(min_row=2, values_only=True):
        total += 1
        name = row[employer_idx]
        if not (name and isinstance(name, str)):
            continue
        name = name.strip()
        rec = counts.setdefault(name, {"filing_count": 0, "certified_count": 0, "last_certified": None})
        rec["filing_count"] += 1
        skip = False
        for filter_col, filter_val in filters.items():
            idx = col_idx.get(filter_col)
            if idx is not None and row[idx] != filter_val:
                skip = True
                break
        if skip:
            continue
        rec["certified_count"] += 1
        filtered += 1
        employers.add(name)
        d = iso_date(row[date_idx]) if date_idx is not None else None
        if d and (rec["last_certified"] is None or d > rec["last_certified"]):
            rec["last_certified"] = d
        if soc_idx is not None:
            soc_certified += 1
            raw_code = row[soc_idx]
            if raw_code:
                code = str(raw_code).split(".")[0].strip()
                if code:
                    soc_with += 1
                    empsoc = soc_counts.setdefault(name, {})
                    empsoc[code] = empsoc.get(code, 0) + 1

    wb.close()
    filter_desc = ", ".join(f"{k}={v}" for k, v in filters.items())
    print(f"  Rows: {total:,}, {filter_desc}: {filtered:,}, Unique employers: {len(employers):,}")
    return employers, counts, {"certified": soc_certified, "with_soc": soc_with}, soc_counts


def cap_soc(prefixes: dict, top: int = SOC_TOP) -> dict:
    """Top-N SOC prefixes per employer, deterministic (count desc, then code asc)."""
    return dict(sorted(prefixes.items(), key=lambda kv: (-kv[1], kv[0]))[:top])


def normalize_employer_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower().strip()
    n = re.sub(
        r"\b(inc|llc|ltd|corp|co|lp|llp|plc|gmbh|ag|sa|nv|bv|pte|pvt|limited|incorporated|corporation|company|group|holdings?|technologies?|solutions?|services?|systems?)\.?",
        "",
        n,
    )
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def parse_quarter(q: str) -> tuple[int, int]:
    m = re.match(r"FY(\d{4})_Q(\d)", q)
    if not m:
        raise ValueError(f"Invalid quarter format: {q}. Expected FYXXXX_QN")
    return int(m[1]), int(m[2])


def next_quarter(q: str) -> str:
    fy, qn = parse_quarter(q)
    qn += 1
    if qn > 4:
        qn = 1
        fy += 1
    return f"FY{fy}_Q{qn}"


def build_quarter_window(latest: str, window: int) -> list[str]:
    quarters = [latest]
    fy, qn = parse_quarter(latest)
    for _ in range(window - 1):
        qn -= 1
        if qn < 1:
            qn = 4
            fy -= 1
        quarters.append(f"FY{fy}_Q{qn}")
    return quarters


def load_existing(path: Path) -> dict:
    if not path.exists():
        return {"_meta": {}, "employers": []}
    with open(path) as f:
        return json.load(f)


def detect_latest_available() -> str | None:
    """Auto-detect the latest quarter with DOL data published.
    DOL FY starts October: Q1=Oct-Dec, Q2=Jan-Mar, Q3=Apr-Jun, Q4=Jul-Sep.
    DOL publishes ~2-3 months after quarter end, so the current quarter
    is often not yet available — walks backwards until a 200 is found."""
    now = datetime.datetime.now()
    fiscal_month = (now.month - 10) % 12  # Oct=0, Nov=1, ... Sep=11
    qn = fiscal_month // 3 + 1
    fy = now.year + 1 if now.month >= 10 else now.year  # FY designation (not start year)

    for _ in range(6):  # Try up to 6 quarters back (DOL can lag ~6 months)
        candidate = f"FY{fy}_Q{qn}"
        url = DOL_BASE_URL.format(prefix="LCA", quarter=candidate)  # INF-R2-STALERELIC-CLUSTER-1: detect path predates the {prefix} template param — KeyError'd every --auto run
        print(f"Auto-detect: checking {candidate}...")
        try:
            resp = requests.head(url, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                print(f"  {candidate} available")
                return candidate
            print(f"  {candidate}: HTTP {resp.status_code}")
        except requests.RequestException as e:
            print(f"  {candidate}: {e}", file=sys.stderr)
        qn -= 1
        if qn < 1:
            qn = 4
            fy -= 1

    return None


def validate_match_rate(old_employers: set[str], new_employers: set[str], threshold: float = 0.02) -> bool:
    if not old_employers:
        print("  No existing data to compare against — skipping validation")
        return True

    old_norm = {normalize_employer_name(e) for e in old_employers}
    new_norm = {normalize_employer_name(e) for e in new_employers}

    retained = old_norm & new_norm
    retention_rate = len(retained) / len(old_norm) if old_norm else 0

    print(f"  Previous: {len(old_employers):,} employers ({len(old_norm):,} normalized)")
    print(f"  New:      {len(new_employers):,} employers ({len(new_norm):,} normalized)")
    print(f"  Retained: {len(retained):,} ({retention_rate:.1%})")
    print(f"  Added:    {len(new_norm - old_norm):,}")
    print(f"  Removed:  {len(old_norm - new_norm):,}")

    if retention_rate < (1 - threshold):
        print(f"  WARNING: Retention rate {retention_rate:.1%} is below {(1-threshold):.0%} threshold", file=sys.stderr)
        print(f"  This may indicate a normalization regression or data format change", file=sys.stderr)
        return False

    return True



def run_selftest() -> None:
    """Synthetic-fixture check of the SOC extraction + top-16 cap (no network).
    AGG-LCA-SOC-EXTRACTOR-MISSING-1: the 09-07 implementation was lost with its
    scratch dir; this selftest keeps the re-implementation honest in-repo."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["CASE_STATUS", "VISA_CLASS", "EMPLOYER_NAME", "DECISION_DATE", "SOC_CODE"])
    data = [
        ("Certified", "H-1B", "Acme Inc", "2026-01-05", "15-1252.00"),
        ("Certified", "H-1B", "Acme Inc", "2026-01-06", "15-1252.00"),
        ("Certified", "H-1B", "Acme Inc", "2026-01-07", "15-1251.00"),
        ("Certified", "H-1B", "Acme Inc", "2026-01-08", "13-2011.00"),
        ("Denied", "H-1B", "Acme Inc", "2026-01-09", "15-1252.00"),   # filtered: not certified
        ("Certified", "H-1B", "Acme Inc", "2026-01-10", None),        # certified, no code
        ("Certified", "H-1B", "Beta LLC", "2026-02-01", "15-1132.00"),
    ]
    data += [("Certified", "H-1B", "Gamma Corp", "2026-02-02", f"{20 + i:02d}-1111.00") for i in range(20)]
    for r in data:
        ws.append(list(r))
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "fixture.xlsx"
        wb.save(p)
        employers, counts, soc_stats, soc_counts = extract_employers(p)
    assert employers == {"Acme Inc", "Beta LLC", "Gamma Corp"}, employers
    assert soc_stats == {"certified": 26, "with_soc": 25}, soc_stats
    assert soc_counts["Acme Inc"] == {"15-1252": 2, "15-1251": 1, "13-2011": 1}, soc_counts.get("Acme Inc")
    assert soc_counts["Beta LLC"] == {"15-1132": 1}
    assert len(soc_counts["Gamma Corp"]) == 20, "extract returns raw prefix counts (cap is applied at output)"
    capped = cap_soc(soc_counts["Gamma Corp"])
    assert len(capped) == SOC_TOP, f"cap: {len(capped)}"
    assert list(capped) == [f"{20 + i:02d}-1111" for i in range(SOC_TOP)], "cap order: count desc, then code asc"
    assert counts["Acme Inc"]["certified_count"] == 5, counts["Acme Inc"]
    print("  selftest PASS: filter, coverage counters, prefix split, top-16 cap")

def main():
    parser = argparse.ArgumentParser(description="LCA Quarterly Data Refresh")
    parser.add_argument("--quarter", help="Latest quarter to include (e.g., FY2026_Q1)")
    parser.add_argument("--auto", action="store_true", help="Auto-detect latest available quarter")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW, help=f"Rolling window size (default: {DEFAULT_WINDOW} quarters)")
    parser.add_argument("--output", default="lca-sponsors.json", help="Output file path")
    parser.add_argument("--existing", help="Path to existing lca-sponsors.json (for validation comparison only)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--selftest", action="store_true", help="Run the synthetic SOC-extraction fixture (no network) and exit")
    args = parser.parse_args()

    if args.selftest:
        run_selftest()
        return

    if not args.quarter and not args.auto:
        parser.error("Specify --quarter or --auto")

    # Determine the latest quarter
    latest = args.quarter or detect_latest_available()
    if not latest:
        print("ERROR: No recent quarter data available from DOL", file=sys.stderr)
        sys.exit(1)

    # Build the rolling window of N quarters ending at latest
    quarters = build_quarter_window(latest, args.window)
    print(f"\n=== DOL Sponsor Data Refresh (LCA + PERM) ===")
    print(f"Latest quarter: {latest}")
    print(f"Window ({args.window} quarters): {quarters}\n")

    # Download and extract employers from ALL quarters in the window.
    # Two sources: LCA (H-1B, large ~72MB/quarter) + PERM (green card, ~11MB/quarter).
    # Both rebuilt from scratch each run — no merge with existing.
    all_employers: set[str] = set()
    all_counts: dict[str, dict] = {}
    soc_by_employer: dict[str, dict[str, int]] = {}      # AGG-LCA-SOC-EXTRACTOR-MISSING-1: certified H-1B rows
    soc_coverage = {"certified": 0, "with_soc": 0}
    perm_soc_by_employer: dict[str, dict[str, int]] = {} # guarded: PERM file has no SOC_CODE today
    perm_soc_coverage = {"certified": 0, "with_soc": 0}

    def merge_counts(counts: dict[str, dict]) -> None:
        for name, rec in counts.items():
            agg = all_counts.setdefault(name, {"filing_count": 0, "certified_count": 0, "last_certified": None})
            agg["filing_count"] += rec["filing_count"]
            agg["certified_count"] += rec["certified_count"]
            if rec["last_certified"] and (agg["last_certified"] is None or rec["last_certified"] > agg["last_certified"]):
                agg["last_certified"] = rec["last_certified"]

    def merge_soc(stats: dict, counts: dict, by_employer: dict, coverage: dict) -> None:
        coverage["certified"] += stats.get("certified", 0)
        coverage["with_soc"] += stats.get("with_soc", 0)
        for name, prefixes in counts.items():
            agg = by_employer.setdefault(name, {})
            for code, n in prefixes.items():
                agg[code] = agg.get(code, 0) + n

    with tempfile.TemporaryDirectory() as tmpdir:
        for q in quarters:
            # LCA (H-1B)
            lca_path = Path(tmpdir) / f"LCA_Disclosure_Data_{q}.xlsx"
            if download_quarter(q, lca_path, prefix="LCA"):
                employers, counts, soc_stats, soc_counts = extract_employers(lca_path, employer_col=EMPLOYER_COL, filters=LCA_FILTER)
                all_employers |= employers
                merge_counts(counts)
                merge_soc(soc_stats, soc_counts, soc_by_employer, soc_coverage)
            elif q == latest:
                print(f"\nERROR: Could not download latest LCA quarter {q}", file=sys.stderr)
                sys.exit(1)
            else:
                print(f"  WARNING: LCA {q} unavailable — window may be smaller\n", file=sys.stderr)

            # PERM (green card)
            perm_path = Path(tmpdir) / f"PERM_Disclosure_Data_{q}.xlsx"
            if download_quarter(q, perm_path, prefix="PERM"):
                employers, counts, soc_stats, soc_counts = extract_employers(perm_path, employer_col=PERM_EMPLOYER_COL, filters=PERM_FILTER)
                all_employers |= employers
                merge_counts(counts)
                merge_soc(soc_stats, soc_counts, perm_soc_by_employer, perm_soc_coverage)
            else:
                print(f"  (PERM {q} not available — LCA-only for this quarter)\n", file=sys.stderr)

            print(f"  Running total: {len(all_employers):,} unique employers\n")

    if not all_employers:
        print("ERROR: No employers extracted from any quarter", file=sys.stderr)
        sys.exit(1)

    # Load existing data for validation (comparison only — output is built from scratch)
    existing_path = Path(args.existing) if args.existing else Path(args.output)
    existing_data = load_existing(existing_path)
    existing_employers = set(existing_data.get("employers", []))
    existing_quarters = existing_data.get("_meta", {}).get("quarters", [])

    print(f"Existing data: {len(existing_employers):,} employers from {existing_quarters}")

    # Validate retention rate (sanity check — informational, not a hard gate)
    print("\nValidation:")
    if existing_employers:
        validate_match_rate(existing_employers, all_employers)

    # Counts companion (INF-LCA-COUNTS-INPUT-1): keyed by the SAME raw employer strings
    # as employers[] (certified set only), so readers join trivially and the names-only
    # path stays intact. filing_count >= certified_count always.
    employer_counts = {name: all_counts[name] for name in sorted(all_employers) if name in all_counts}

    # SOC companion (AGG-LCA-SOC-EXTRACTOR-MISSING-1 / design §6.1): per-employer 4-digit
    # SOC prefix counts over certified H-1B rows, top-16 cap per employer (deterministic:
    # count desc, then code asc). Keys = raw employers[] strings — the consumer
    # (job-board-processing enrich/visa.js buildLcaSocTech) normalizes both name forms itself.
    def top_soc(by_employer: dict) -> dict:
        out: dict[str, dict[str, int]] = {}
        for name in sorted(all_employers):
            prefixes = by_employer.get(name)
            if not prefixes:
                continue
            out[name] = cap_soc(prefixes)
        return out

    employer_soc_counts = top_soc(soc_by_employer)
    employer_soc_counts_perm = top_soc(perm_soc_by_employer)  # stays {} until PERM carries SOC_CODE
    lca_cov_pct = round(soc_coverage["with_soc"] / soc_coverage["certified"] * 100, 1) if soc_coverage["certified"] else 0.0

    # Build output
    output = {
        "_meta": {
            "description": "DOL OFLC certified employer names — LCA (H-1B) + PERM (green card), rolling window",
            "source": "DOL OFLC LCA + PERM Disclosure Data",
            "quarters": quarters,
            "generated": datetime.datetime.now().isoformat()[:10],
            "filter": "LCA: CASE_STATUS=Certified, VISA_CLASS=H-1B | PERM: CASE_STATUS=Certified",
            "total_employers": len(all_employers),
            "format": "employers[] name-only (backward compatible with enrich-jobs.js loadLcaSponsors) + employer_counts{} per-employer {filing_count, certified_count, last_certified} + employer_soc_counts{} per-employer {SOC_PREFIX: count} top-16 over certified H-1B rows",
            "counts_note": "filing_count = all rows for the employer in the window (any status/class); certified_count = rows passing the filter; last_certified = max DECISION_DATE among certified rows; keys = employers[] entries",
            "soc_note": "employer_soc_counts = 4-digit SOC prefix counts over CERTIFIED H-1B rows only, top-16 per employer (ENR v116 occupation-gate input); employer_soc_counts_perm guarded — the PERM disclosure file carries no SOC_CODE column (verified 09-07)",
            "soc_coverage": {
                "lca": {
                    "certified": soc_coverage["certified"],
                    "with_soc": soc_coverage["with_soc"],
                    "coverage_pct": lca_cov_pct,
                    "employers_with_soc": len(employer_soc_counts),
                },
                "perm": {
                    "certified": perm_soc_coverage["certified"],
                    "with_soc": perm_soc_coverage["with_soc"],
                    "note": "no SOC_CODE column in PERM disclosure" if perm_soc_coverage["certified"] == 0 else "ok",
                },
            },
        },
        "employers": sorted(all_employers),
        "employer_counts": employer_counts,
        "employer_soc_counts": employer_soc_counts,
        "employer_soc_counts_perm": employer_soc_counts_perm,
    }

    if args.dry_run:
        print(f"Would write {len(all_employers):,} employers (+ employer_counts for {len(employer_counts):,}) to {args.output}")
        print(f"Quarters: {quarters}")
        if existing_employers:
            delta_added = len(all_employers - existing_employers)
            delta_removed = len(existing_employers - all_employers)
            print(f"New employers: +{delta_added:,}")
            print(f"Removed employers: -{delta_removed:,}")
        print(f"Meta: {json.dumps(output['_meta'], indent=2)}")
        return

    # Write output
    out_path = Path(args.output)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    size_kb = out_path.stat().st_size / 1024
    print(f"\nWritten to {out_path} ({size_kb:.0f} KB)")

    print(f"\nNext step: upload to R2 (data/lca-sponsors.json)")
    print(f"  aws s3 cp {out_path} s3://$R2_BUCKET_NAME/data/lca-sponsors.json --endpoint-url $R2_ENDPOINT")


if __name__ == "__main__":
    main()
