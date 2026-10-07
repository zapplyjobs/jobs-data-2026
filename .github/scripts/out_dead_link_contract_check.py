#!/usr/bin/env python3
"""OUT-LIFECYCLE-SINGLEAUTHORITY-1 stage-3b: post-publish contract self-check.

Asserts the dead-links.json shapes the zapply-backend transport parsers depend on
(INF decision 3 shared-spec clause: a second drifting implementation is the failure
mode - this pins the PRODUCER side; the worker vectors in deadFlip.test.ts pin the
CONSUMER side). Run after publish; exit 1 on any violation so the workflow surfaces
a contract break the same run instead of silently zeroing the transport.

Contract checked (worker: deadFlip.ts parseDeadAtsUrls / parseNobsDeaths /
fetchDeadFlipTargets):
  - generated_at: present and ISO-8601 parseable (the reversibility guard requires it)
  - dead_links rows: url (http-starting string) + http_code ('ats-closed' strings only
    are admitted worker-side; int 404/410 rows are data the worker reads via
    http_sightings instead - both shapes must parse)
  - http_sightings entries: code int, sightings int, first_seen/last_seen strings
Usage: python3 out_dead_link_contract_check.py <dead-links.json path>
"""
import json
import sys
from datetime import datetime

def fail(msg):
    print(f"CONTRACT VIOLATION: {msg}")
    sys.exit(1)

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "scan_summary.json"
    with open(path, "r", encoding="utf-8") as f:
        artifact = json.load(f)

    generated_at = artifact.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        fail("generated_at missing or not a string")
    try:
        datetime.fromisoformat(generated_at)
    except ValueError:
        fail(f"generated_at not ISO-8601 parseable: {generated_at!r}")

    dead_links = artifact.get("dead_links")
    if not isinstance(dead_links, list):
        fail("dead_links missing or not a list")
    for i, row in enumerate(dead_links):
        if not isinstance(row, dict):
            fail(f"dead_links[{i}] not an object")
        url = row.get("url")
        code = row.get("http_code")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            fail(f"dead_links[{i}].url not an http(s) string: {url!r}")
        # http_code is a STRING for ats-closed rows and an INT for HTTP-class N-observation
        # deaths (out_dead_link_scan.py emits int(s.get('code')) once sightings >= 2). Both
        # shapes are producer-canonical; the worker's deadFlip.test.ts pins 'both shapes
        # must parse'. Ship-review P1 fix 2026-10-07: the first draft required str-only and
        # would have false-redned the first run with an HTTP-class death.
        if not isinstance(code, (str, int)) or isinstance(code, bool):
            fail(f"dead_links[{i}].http_code neither string nor int: {code!r}")

    sightings = artifact.get("http_sightings")
    if sightings is None:
        fail("http_sightings missing (transport N-obs admission reads it)")
    if not isinstance(sightings, dict):
        fail("http_sightings not an object")
    for url, rec in sightings.items():
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            fail(f"http_sightings key not an http(s) string: {url!r}")
        if not isinstance(rec, dict):
            fail(f"http_sightings[{url!r}] not an object")
        code = rec.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            fail(f"http_sightings[{url!r}].code not an int (worker admits int 404/410 only): {code!r}")
        n = rec.get("sightings")
        if not isinstance(n, int) or isinstance(n, bool):
            fail(f"http_sightings[{url!r}].sightings not an int: {n!r}")

    print(f"dead-links contract OK: {len(dead_links)} dead_links rows, "
          f"{len(sightings)} http_sightings entries, generated_at {generated_at}")

if __name__ == "__main__":
    main()
