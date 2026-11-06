#!/usr/bin/env python3
"""sup-signals-health - freshness + schema health for SUP's published runtime signals.

D5 named instrument (SUP_DOMAIN_MAP_2026_10_20.md, instrument-script class per the
s8 ladder - first recurrence, NOT a kit). Checks each published artifact for:
  1. presence,
  2. schema_version present (int >= 1),
  3. its timestamp field present and within 2x its publish cadence.

Reads R2 directly (same env contract as the tranche reader:
R2_ENDPOINT/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET_NAME).
Exit 0 = all healthy, 1 = findings (alarm-ready when wired as a decay-workflow
step; that wiring is PENDING - jobs-data-2026 is a public repo, config pushes
are bright-line, so the step insertion needs the operator/INF word).

Usage:
  source ~/.secrets/r2-zjp.env && python3 sup-signals-health.py [--json]
  python3 sup-signals-health.py --selftest   # offline mechanics test, no network
"""
import json
import sys
import os
from datetime import datetime, timezone

ARTIFACTS = [
    # key, timestamp field, max age hours (= 2x cadence per the domain map)
    ("data/sup-company-posture.json", "generated_at", 48),      # daily 06:00Z decay workflow
    ("data/sup-posting-ledger-stats.json", "generated_at", 48),  # same daily publish
    ("data/sup_tenants.json", "generated_at", 336),              # per mutation batch; 336h = 2x the ~weekly effective publish rhythm, DELIBERATE slack so quiet weeks don't false-alarm (F314 review)
    ("data/sup_tier2_employers.json", "updated", 2160),          # wave/quarterly (90d)
]


def _parse_ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def check_artifact(s3, bucket, key, ts_field, max_age_h, now):
    try:
        o = s3.get_object(Bucket=bucket, Key=key)
        body = o["Body"].read().decode("utf-8")
    except Exception as e:
        return [f"{key}: UNREADABLE ({type(e).__name__})"]
    out = []
    try:
        doc = json.loads(body)
    except Exception as e:
        return [f"{key}: not valid JSON ({e})"]
    sv = doc.get("schema_version")
    if not isinstance(sv, int) or sv < 1:
        out.append(f"{key}: schema_version missing/invalid ({sv!r})")
    ts = doc.get(ts_field)
    if not ts:
        out.append(f"{key}: timestamp field '{ts_field}' missing")
        return out
    try:
        age_h = (now - _parse_ts(ts)).total_seconds() / 3600.0
    except Exception as e:
        return [f"{key}: timestamp '{ts}' unparseable ({e})"]
    if age_h < -1:
        out.append(f"{key}: timestamp in the future ({ts})")
    elif age_h > max_age_h:
        out.append(f"{key}: stale - {age_h:.0f}h old > {max_age_h}h budget (ts {ts})")
    return out


def selftest():
    from types import SimpleNamespace
    now = datetime.now(timezone.utc)
    ok = True

    def fake_s3(bodies):
        def get_object(Bucket, Key):
            if Key not in bodies:
                raise KeyError(Key)
            return {"Body": SimpleNamespace(read=lambda b=bodies[Key]: b.encode("utf-8"))}
        return SimpleNamespace(get_object=get_object)

    fresh = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    old = "2026-01-01T00:00:00Z"
    cases = [
        ("healthy", json.dumps({"schema_version": 1, "generated_at": fresh}), 48, []),
        ("no schema", json.dumps({"generated_at": fresh}), 48, ["schema_version"]),
        ("stale", json.dumps({"schema_version": 2, "generated_at": old}), 48, ["stale"]),
        ("no ts", json.dumps({"schema_version": 1}), 48, ["timestamp field"]),
        ("bad json", "not json{", 48, ["not valid JSON"]),
    ]
    for name, body, budget, needles in cases:
        got = check_artifact(fake_s3({"data/x.json": body}), "b", "data/x.json", "generated_at", budget, now)
        hit = all(any(nd in g for g in got) for nd in needles) and (len(got) == len(needles))
        print(f"selftest {'PASS' if hit else 'FAIL'}: {name} -> {got}")
        ok = ok and hit
    print(f"sup-signals-health selftest: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:  # grandfathered alias; canonical flag is --self-test (KIT_STANDARD s5)
        argv[argv.index("--selftest")] = "--self-test"
    if "--self-test" in argv:
        return selftest()
    try:
        import boto3
    except ImportError:
        print("sup-signals-health: boto3 unavailable", file=sys.stderr)
        return 2
    need = [v for v in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME") if not os.environ.get(v)]
    if need:
        print(f"sup-signals-health: missing env {need} (source ~/.secrets/r2-zjp.env)", file=sys.stderr)
        return 2
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
    now = datetime.now(timezone.utc)
    findings = []
    for key, ts_field, max_age_h in ARTIFACTS:
        f = check_artifact(s3, os.environ["R2_BUCKET_NAME"], key, ts_field, max_age_h, now)
        findings.extend(f)
        print(f"{'OK  ' if not f else 'FIND'} {key}")
        for line in f:
            print(f"     {line}")
    print(f"sup-signals-health: {len(findings)} finding(s) across {len(ARTIFACTS)} artifacts")
    if "--json" in argv:
        print(json.dumps({"generated_at": now.isoformat(timespec="seconds"), "findings": findings}))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
