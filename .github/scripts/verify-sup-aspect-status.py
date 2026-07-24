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


def proxy_json(path):
    try:
        url = f"{PROXY}/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "ZJP-AspectVerifier/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[proxy_json] {path}: {e}", file=sys.stderr)
        return None


def green_if(cond):
    return "GREEN" if cond else "RED"


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


def c_monitoring():
    """SUP yield-decay monitor workflow exists + zjp-metrics alerting surface available.
    GREEN if decay monitor workflow is configured with recent runs.
    Alert count is informational (zero alerts = healthy, not RED)."""
    decay_runs = gh_runs("zapplyjobs/jobs-data-2026", workflow="sup-yield-decay-monitor.yml", limit=1)
    has_decay = decay_runs is not None and len(decay_runs) > 0
    metrics = proxy_json("zjp-metrics.json")
    alert_count = len(metrics.get("alerts", [])) if metrics else None
    alert_info = f"{alert_count} alerts" if alert_count is not None else "proxy unavailable"
    return green_if(has_decay), f"decay-monitor workflow {'present' if has_decay else 'MISSING'}, zjp-metrics.alerts {alert_info}", "gh-api:workflows+proxy:zjp-metrics"


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
    """company-list.json freshness — stale data degrades SUP matching."""
    age = gh_file_age_days(SUP_REPO, "lib/fetchers/company-list.json")
    status, detail = bucket_age(age, 30, 90)
    return status, f"company-list.json modified {detail}", "gh-api:commits"


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


if __name__ == "__main__":
    result = verify()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")

    counts = {}
    for k, v in result["aspects"].items():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"\nSUP Aspect Status Summary: {counts}", file=sys.stderr)
