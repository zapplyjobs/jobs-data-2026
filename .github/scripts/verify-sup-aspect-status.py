#!/usr/bin/env python3
"""
SUP Aspect Status (verified) — fully CI-native verifier for the Quality Matrix.

ALL aspects are live-checked via gh-api (repo contents, commits, CI runs,
dependabot) and R2 proxy. No carry-forward, no workspace-file dependencies.

This mirrors the OUT verifier pattern: 100% CI-native, zero carry-forward.
INF-ASPECT-CADENCE-1: wires SUP's aspect-status to a 6h schedule.
INF-ASPECT-CI-NATIVE-1: converted all 5 carry-forward aspects to gh-api checks.
"""
import json, os, subprocess, urllib.request, datetime, sys

NOW = datetime.datetime.now(datetime.timezone.utc)
PROXY = "https://zjp-data-proxy.wild-queen-069e.workers.dev/data"
SUP_REPO = "zapplyjobs/job-board-aggregator"
SUP_REPOS = ["zapplyjobs/job-board-aggregator", "zapplyjobs/jobs-aggregator-private"]


def gh_json(args, attempts=3):
    for a in range(attempts):
        try:
            result = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
            if result.returncode != 0 and a < attempts - 1:
                import time; time.sleep(2)
        except Exception:
            if a < attempts - 1:
                import time; time.sleep(2)
    return None


def gh_runs(repo, workflow=None, limit=5):
    args = ["run", "list", "-R", repo, "-L", str(limit), "--json", "status,conclusion,createdAt,updatedAt"]
    if workflow:
        args = ["run", "list", "-R", repo, "-w", workflow, "-L", str(limit), "--json", "status,conclusion,createdAt,updatedAt"]
    rows = gh_json(args) or []
    return [r for r in rows if r.get("status") == "completed"]


def r2_get_json(key):
    """Read a JSON artifact from R2 (same creds publish_r2 uses).
    Returns (data, None) | (None, 'missing') | (None, 'error: <msg>')."""
    try:
        import boto3
        s3 = boto3.client("s3", region_name="auto",
                          endpoint_url=os.environ["R2_ENDPOINT"],
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
        return json.loads(s3.get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key)["Body"].read()), None
    except Exception as e:
        if "NoSuchKey" in e.__class__.__name__ or "NoSuchKey" in str(e):
            return None, "missing"
        return None, f"error: {e}"


def proxy_json(path):
    try:
        url = f"{PROXY}/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "ZJP-AspectVerifier/1.0", "X-Proxy-Token": os.environ.get("DATA_PROXY_TOKEN", "")})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[proxy_json] {path}: {e}", file=sys.stderr)
        return None


def green_if(cond):
    return "GREEN" if cond else "RED"

VALID_ATS = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "oracle", "tiktok", "deshaw", "custom-supplemental"}




def validate_company_list(data):
    """Structural validation of company-list.json — Python port of
    projects/zjp/scripts/sup-company-list-validate.js. Returns (errors, warnings,
    tenant_count, ats_count). GREEN iff no errors (scope-variant warnings don't fail).
    INF-ASPECT-DATAQUALITY-VALIDATOR-1."""
    errors, warnings = [], []
    if not isinstance(data, dict):
        return ["company-list is not a JSON object"], [], 0, 0
    meta = data.get("_meta")
    if not meta:
        errors.append("_meta key missing")
    else:
        if not meta.get("version"):
            warnings.append("_meta.version missing")
        if not meta.get("updated"):
            warnings.append("_meta.updated missing")
    ats_keys = [k for k in data if k != "_meta"]
    total = 0
    for ats in ats_keys:
        if ats not in VALID_ATS:
            errors.append('Unknown ATS platform: "%s"' % ats)
            continue
        tenants = data[ats]
        if not isinstance(tenants, list):
            errors.append('ATS "%s" is not an array' % ats)
            continue
        total += len(tenants)
        seen = {}
        for i, t in enumerate(tenants):
            name = t.get("name") if isinstance(t, dict) else ""
            if not name or not str(name).strip():
                errors.append("[%s][%d] missing or empty name" % (ats, i))
                continue
            id_field = "url" if ats == "workday" else ("base_url" if ats == "oracle" else "slug")
            idv = t.get(id_field)
            if not idv or not str(idv).strip():
                errors.append('[%s][%d] "%s" missing required field "%s"' % (ats, i, name, id_field))
                continue
            if idv in seen:
                if seen[idv] == name:
                    errors.append('[%s] exact duplicate: id="%s" name="%s"' % (ats, idv, name))
                else:
                    warnings.append('[%s] scope variant: id="%s" has names "%s" + "%s"' % (ats, idv, seen[idv], name))
            else:
                seen[idv] = name
    return errors, warnings, total, len(ats_keys)


# --- CI-runnable checks ---

def c_verification():
    """CI Gate on job-board-aggregator succeeds (validates company-list + repo)."""
    runs = gh_runs(SUP_REPO, workflow="ci-gate.yml", limit=3)
    if not runs:
        return "RED", "no completed ci-gate runs found", "gh-api:CI"
    fails = [r for r in runs if r.get("conclusion") != "success"]
    latest = runs[0]
    status = green_if(len(fails) == 0 and latest.get("conclusion") == "success")
    return status, f"last ci-gate {latest.get('conclusion')} ({latest['createdAt'][:10]}), {len(fails)} fail in last 3", "gh-api:CI"


def _artifact_staleness(label, key):
    """Shared freshness band for R2 output artifacts (writer-death alarms).
    Returns (status, detail): RED missing/>48h/unparseable; YELLOW 24-48h or transient
    read error; GREEN fresh. A monitoring check must not storm RED on its own transport
    flakes — transient errors degrade to YELLOW and the next hourly run corrects."""
    data, err = r2_get_json(key)
    if err == "missing":
        return "RED", f"{label} artifact MISSING (writer dead?)"
    if err is not None:
        return "YELLOW", f"{label} read failed ({err})"
    gen = (data or {}).get("generated_at")
    try:
        age_h = (NOW - datetime.datetime.fromisoformat(str(gen).replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:
        return "RED", f"{label} generated_at unparseable"
    if age_h > 48:
        return "RED", f"{label} {age_h:.0f}h old (>48h — writer dead?)"
    if age_h > 24:
        return "YELLOW", f"{label} {age_h:.0f}h old (aging)"
    return "GREEN", f"{label} {age_h:.1f}h old"


def c_monitoring():
    """SUP yield-decay monitor workflow + OUTPUT-freshness legs (ledger + decay report)
    + zjp-metrics alerts. Output artifacts get writer-death alarms because input
    freshness gating cannot see a green-but-empty run (SUP-LEDGER-FRESHNESS-MON-1,
    SUP-DECAYOUT-FRESH-1). Bands: RED missing/>48h/unparseable or workflow missing;
    YELLOW 24-48h or transient read error."""
    decay_runs = gh_runs("zapplyjobs/jobs-data-2026", workflow="sup-yield-decay-monitor.yml", limit=1)
    has_decay = decay_runs is not None and len(decay_runs) > 0
    zjp = proxy_json("zjp-metrics.json")
    alerts = (zjp or {}).get("alerts")
    alert_info = f"{len(alerts)} open" if isinstance(alerts, list) else "n/a"

    ledger_status, ledger_detail = _artifact_staleness("ledger-stats", "data/sup-posting-ledger-stats.json")
    decay_status, decay_detail = _artifact_staleness("decay-report", "data/sup_yield_decay_monitor.json")

    parts = [f"decay-monitor workflow {'present' if has_decay else 'MISSING'}",
             ledger_detail,
             decay_detail,
             f"zjp-metrics.alerts {alert_info}"]
    if not has_decay or "RED" in (ledger_status, decay_status):
        status = "RED"
    elif "YELLOW" in (ledger_status, decay_status):
        status = "YELLOW"
    else:
        status = "GREEN"
    return status, "; ".join(parts), "gh-api:workflows+r2:ledger+decay+proxy:zjp-metrics"


def c_security():
    """No critical/high Dependabot alerts across SUP repos."""
    crit = 0
    opens = 0
    for repo in SUP_REPOS:
        alerts = gh_json(["api", f"/repos/{repo}/dependabot/alerts?state=open&per_page=50"]) or []
        opens += len(alerts)
        crit += sum(1 for a in alerts if a.get("security_advisory", {}).get("severity") in ("critical", "high"))
    return green_if(crit == 0), f"{opens} open alerts, {crit} critical/high", "gh-api:dependabot"


# --- CI-native checks (all aspects live — no carry-forward) ---

def gh_file_age_days(repo, filepath):
    """Days since a file was last modified, via commits API."""
    commits = gh_json(["api", f"/repos/{repo}/commits?path={filepath}&per_page=1"]) or []
    if commits:
        date = commits[0].get("commit", {}).get("author", {}).get("date", "")
        if date:
            return (NOW - datetime.datetime.fromisoformat(date.replace("Z", "+00:00"))).days
    return None


def bucket_age(age, green_days, yellow_days):
    if age is None:
        return "RED", "age unknown"
    if age <= green_days:
        return "GREEN", f"{age}d ago"
    if age <= yellow_days:
        return "YELLOW", f"{age}d ago"
    return "RED", f"{age}d ago"


def c_data_quality():
    """company-list.json STRUCTURAL validity (content accuracy — not just freshness).
    Ports sup-company-list-validate.js: valid JSON, _meta.version, known ATS, required
    fields, no dup slugs. Reads the checked-out + transcrypt-unlocked copy (the file is
    encrypted at rest, so it cannot be validated via gh-api). The workflow checks out
    job-board-aggregator at _agg/ + unlocks it. Falls back to freshness if the checkout
    is absent or validate fails. INF-ASPECT-DATAQUALITY-VALIDATOR-1."""
    local = os.path.join(os.getcwd(), "_agg", "lib", "fetchers", "company-list.json")
    if os.path.exists(local):
        try:
            data = json.load(open(local))
            errors, warnings, total, ats_n = validate_company_list(data)
            ok = len(errors) == 0
            status = "GREEN" if ok else "RED"
            detail = f"validator {'PASS' if ok else 'FAIL'}: {total} tenants / {ats_n} ATS; {len(errors)} err, {len(warnings)} warn"
            if errors:
                detail += " — " + "; ".join(errors[:2])
            return status, detail, "fs:company-list+sup-company-list-validate"
        except Exception as e:
            print(f"[c_data_quality] local validate failed: {e}", file=sys.stderr)
    age = gh_file_age_days(SUP_REPO, "lib/fetchers/company-list.json")
    status, detail = bucket_age(age, 30, 90)
    return status, f"company-list.json modified {detail} (content-validation unavailable)", "gh-api:commits"


def c_configuration():
    """Config files freshness — stale config indicates neglect."""
    paths = ["lib/fetchers/company-list.json", ".github/workflows/ci-gate.yml"]
    ages = [gh_file_age_days(SUP_REPO, p) for p in paths]
    valid = [a for a in ages if a is not None]
    if not valid:
        return "RED", "cannot determine config ages", "gh-api:commits"
    max_age = max(valid)
    status, _ = bucket_age(max_age, 90, 180)
    return status, f"oldest config file {max_age}d old ({len(valid)}/{len(paths)} files)", "gh-api:commits"


def c_discoverability():
    """README freshness — stale README means stale public face."""
    age = gh_file_age_days(SUP_REPO, "README.md")
    status, detail = bucket_age(age, 90, 180)
    return status, f"README modified {detail}", "gh-api:commits"


def c_documentation():
    """Recent commit activity — documentation freshness proxy."""
    commits = gh_json(["api", f"/repos/{SUP_REPO}/commits?per_page=1"]) or []
    if commits:
        last_date = commits[0].get("commit", {}).get("author", {}).get("date", "")
        if last_date:
            age = (NOW - datetime.datetime.fromisoformat(last_date.replace("Z", "+00:00"))).days
            status = "GREEN" if age <= 30 else ("YELLOW" if age <= 90 else "RED")
            return status, f"last commit {age}d ago — activity proxy", "gh-api:commits"
    return "RED", "no commits found", "gh-api:commits"


def c_change_mgmt():
    """ci-gate triggers on push — SDLC enforcement proxy (non-redundant with verification).
    Verification checks the RESULT (pass/fail); this checks ENFORCEMENT (auto-trigger)."""
    import base64
    content = gh_json(["api", f"/repos/{SUP_REPO}/contents/.github/workflows/ci-gate.yml"])
    if not content or "message" in content:
        return "RED", "ci-gate.yml not found", "gh-api:repo-contents"
    yaml_text = base64.b64decode(content.get("content", "")).decode("utf-8", errors="replace")
    has_push = "push:" in yaml_text
    return green_if(has_push), f"ci-gate {'auto-triggers on push' if has_push else 'manual-only'} — enforcement proxy", "gh-api:repo-contents"


CI_CHECKS = {
    "verification": c_verification,
    "monitoring": c_monitoring,
    "security": c_security,
    "data_quality": c_data_quality,
    "configuration": c_configuration,
    "discoverability": c_discoverability,
    "documentation": c_documentation,
    "change_mgmt": c_change_mgmt,
}


def verify():
    aspects = {}
    for name, func in CI_CHECKS.items():
        try:
            status, evidence, source = func()
        except Exception as e:
            status, evidence, source = "YELLOW", f"check error: {e}", "error"
        aspects[name] = {"status": status, "evidence": evidence, "source": source}
    return {"module": "SUP", "generated_at": NOW.isoformat(), "aspects": aspects}




def publish_r2(data_str):
    """R2-PRIMARY: publish sup-aspect-status.json to R2 (canonical object store).
    Uses boto3 (proper SigV4 — no hand-rolled signing). Reads R2_* env."""
    import boto3
    s3 = boto3.client("s3", region_name="auto",
                      endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    s3.put_object(Bucket=os.environ["R2_BUCKET_NAME"],
                  Key="data/sup-aspect-status.json",
                  Body=data_str, ContentType="application/json")
    print("published R2: data/sup-aspect-status.json", file=sys.stderr)


if __name__ == "__main__":
    do_publish = "--publish" in sys.argv
    result = verify()
    data_str = json.dumps(result, indent=2)
    sys.stdout.write(data_str + "\n")

    counts = {}
    for k, v in result["aspects"].items():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"\nSUP Aspect Status Summary: {counts}", file=sys.stderr)

    if do_publish:
        try:
            publish_r2(data_str)
        except Exception as e:
            print(f"R2 publish FAILED: {e}", file=sys.stderr)
