#!/usr/bin/env python3
"""
verify-inf-aspect-status.py — MACHINE-VERIFIED INF aspect checks.

Per ASPECT_STATUS_CONTRACT (DASH-QUALITYMATRIX-ACTIONABILITY-1):
- VERIFIED model: each aspect = objective machine check on a live signal (no self-assessment).
- 10 verified aspects eligible for the grid; content_quality + coordination are NARRATIVE
  (omitted — they are subjective, not machine-checkable).
- CI-native: every aspect live-checked via gh-api + Worker proxy (zero carry-forward,
  per the ADOPTED carry-forward fix — workspace aspects redefined to repo-state checks).

INF scope (INF-ASPECT-VERIFIER-1; supersedes the manual inf-aspect-status.json artifact):
- performance measures INF-infra latency (Worker proxy + dashboard /api/data), NOT AGG
  pipeline runtime (AGG owns that — AGG-ASPECT-PERF-OWNER-1).
- data_quality measures INF's published data-pipeline freshness (zjp-metrics.json age),
  not AGG's pool retrievable rate.

Published to data/inf-aspect-status.json (R2) by publish-inf-aspect-status.yml each cycle.
"""
import json, os, subprocess, urllib.request, datetime, sys, time

NOW = datetime.datetime.now(datetime.timezone.utc)
PROXY = "https://zjp-data-proxy.wild-queen-069e.workers.dev"
PROXY_DATA = PROXY + "/data"
DASHBOARD_API = "https://dash.zapply.jobs/api/data"

# INF-maintained pipeline repos (where INF's CI gates, configs, scripts live).
INF_REPOS = [
    "zapplyjobs/jobs-data-2026",
    "zapplyjobs/jobs-aggregator-private",
    "zapplyjobs/job-board-processing",
]
# CI gate workflow per repo (verification + change_mgmt checks).
GATE_WORKFLOW = {
    "zapplyjobs/jobs-data-2026": "gate.yml",
    "zapplyjobs/jobs-aggregator-private": "gate.yml",
    "zapplyjobs/job-board-processing": "ci-gate.yml",
}


def gh_json(args, attempts=3):
    for _ in range(attempts):
        try:
            out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                return json.loads(out.stdout)
        except Exception:
            pass
        time.sleep(1)
    return None


def gh_runs(repo, workflow=None, limit=5):
    args = ["run", "list", "-R", repo, "-L", str(limit), "--json", "status,conclusion,createdAt"]
    if workflow:
        args = ["run", "list", "-R", repo, "-w", workflow, "-L", str(limit),
                "--json", "status,conclusion,createdAt"]
    rows = gh_json(args) or []
    return [r for r in rows if r.get("status") == "completed"]


def proxy_json(path, timeout=15):
    req = urllib.request.Request(PROXY_DATA + "/" + path,
                                 headers={"User-Agent": "verify-inf-aspect/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[proxy_json] {path}: {e}", file=sys.stderr)
        return None


def green_if(cond):
    return "GREEN" if cond else "RED"


def gh_file_age_days(repo, filepath):
    commits = gh_json(["api", f"/repos/{repo}/commits?path={filepath}&per_page=1"]) or []
    if not commits:
        return None
    ts = commits[0].get("commit", {}).get("author", {}).get("date", "")
    if not ts:
        return None
    try:
        d = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (NOW - d).days
    except Exception:
        return None


def bucket_age(age, green_days, yellow_days):
    if age is None:
        return "RED", "age unknown"
    if age <= green_days:
        return "GREEN", f"{age}d ago"
    if age <= yellow_days:
        return "YELLOW", f"{age}d ago"
    return "RED", f"{age}d ago"


def parse_iso(ts):
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# --- CI-runnable checks (all aspects live — zero carry-forward) ---

def c_verification():
    """CI gates pass on INF pipeline repos."""
    fails, total = 0, 0
    detail = []
    for repo, wf in GATE_WORKFLOW.items():
        runs = gh_runs(repo, workflow=wf, limit=3)
        if not runs:
            detail.append(f"{repo.split('/')[-1]}: no runs")
            fails += 1
            total += 1
            continue
        latest = runs[0]
        total += 1
        if latest.get("conclusion") != "success":
            fails += 1
        detail.append(f"{repo.split('/')[-1]}:{latest.get('conclusion')}")
    status = green_if(fails == 0)
    return status, f"gates {total - fails}/{total} green ({', '.join(detail)})", "gh-api:CI"


def c_monitoring():
    """pipeline-alert.yml exists + recently fired (INF owns pipeline alerting)."""
    runs = gh_runs("zapplyjobs/jobs-data-2026", workflow="pipeline-alert.yml", limit=3)
    wf_exists = bool(gh_json(["api", "/repos/zapplyjobs/jobs-data-2026/actions/workflows/pipeline-alert.yml"]))
    recent = False
    if runs:
        latest = parse_iso(runs[0].get("createdAt", ""))
        if latest:
            recent = (NOW - latest).total_seconds() < 2 * 3600  # fired within 2h
    status = green_if(wf_exists and recent)
    return status, f"pipeline-alert {'present' if wf_exists else 'MISSING'}, last run {'recent' if recent else 'stale/none'}", "gh-api:workflows"


def c_security():
    """No critical/high Dependabot alerts across INF pipeline repos."""
    total_open, crit = 0, 0
    for repo in INF_REPOS:
        alerts = gh_json(["api", f"/repos/{repo}/dependabot/alerts?state=open&per_page=50"]) or []
        total_open += len(alerts)
        for a in alerts:
            sev = (a.get("security_advisory") or {}).get("severity", "")
            if sev in ("critical", "high"):
                crit += 1
    return green_if(crit == 0), f"{total_open} open alerts, {crit} critical/high", "gh-api:dependabot"


def c_data_quality():
    """INF published-data freshness — zjp-metrics.json generated recently (INF's data pipeline)."""
    m = proxy_json("zjp-metrics.json")
    gen = parse_iso((m or {}).get("generated_at", "")) if m else None
    if not gen:
        return "RED", "zjp-metrics.json generated_at missing/unreachable", "proxy:zjp-metrics"
    age_min = (NOW - gen).total_seconds() / 60
    if age_min < 30:
        return "GREEN", f"zjp-metrics.json {int(age_min)}m old", "proxy:zjp-metrics"
    if age_min < 120:
        return "YELLOW", f"zjp-metrics.json {int(age_min)}m old (>30m)", "proxy:zjp-metrics"
    return "RED", f"zjp-metrics.json {int(age_min)}m old (>120m)", "proxy:zjp-metrics"


def c_performance():
    """INF-infra latency — Worker proxy + dashboard /api/data response time.
    Correctly scoped to INF infrastructure (NOT AGG pipeline runtime)."""
    def timed_get(url, timeout=8):
        req = urllib.request.Request(url, headers={"User-Agent": "verify-inf-aspect/1.0"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read()
            return time.time() - t0
        except Exception:
            return None
    proxy_t = timed_get(PROXY_DATA + "/zjp-metrics.json")
    dash_t = timed_get(DASHBOARD_API)
    if proxy_t is None or dash_t is None:
        return "RED", f"unreachable (proxy={'ok' if proxy_t is not None else 'fail'}, dash={'ok' if dash_t is not None else 'fail'})", "proxy+dashboard:latency"
    worst = max(proxy_t, dash_t)
    if worst < 3.0:
        return "GREEN", f"proxy {proxy_t:.2f}s, dashboard /api/data {dash_t:.2f}s", "proxy+dashboard:latency"
    if worst < 8.0:
        return "YELLOW", f"proxy {proxy_t:.2f}s, dashboard /api/data {dash_t:.2f}s (>3s)", "proxy+dashboard:latency"
    return "RED", f"proxy {proxy_t:.2f}s, dashboard /api/data {dash_t:.2f}s (>8s)", "proxy+dashboard:latency"


def c_infrastructure():
    """Deploy health — Worker proxy returns 200 + valid JSON (R2 data path live)."""
    m = proxy_json("zjp-metrics.json")
    if m and isinstance(m, dict) and len(m) > 5:
        return "GREEN", f"Worker proxy 200 + valid JSON ({len(m)} keys); R2 data path live", "proxy:health"
    return "RED", "Worker proxy not returning valid data", "proxy:health"


def c_configuration():
    """INF config freshness — workflow files on pipeline repos not stale (neglect proxy)."""
    ages = []
    for repo in INF_REPOS:
        age = gh_file_age_days(repo, ".github/workflows")
        if age is not None:
            ages.append(age)
    if not ages:
        return "RED", "workflow dir age unknown", "gh-api:commits"
    return bucket_age(max(ages), 60, 180) + ("gh-api:commits",)


def c_discoverability():
    """README freshness on the main pipeline repo (repo-resident discoverability proxy)."""
    age = gh_file_age_days("zapplyjobs/jobs-data-2026", "README.md")
    return bucket_age(age, 90, 365) + ("gh-api:commits",)


def c_documentation():
    """Recent commit activity on INF pipeline scripts (documentation-freshness proxy)."""
    age = gh_file_age_days("zapplyjobs/jobs-data-2026", ".github/scripts")
    return bucket_age(age, 30, 90) + ("gh-api:commits",)


def c_change_mgmt():
    """CI gate auto-triggers on push (SDLC enforcement proxy, non-redundant with verification)."""
    content = gh_json(["api", "/repos/zapplyjobs/jobs-data-2026/contents/.github/workflows/gate.yml"])
    if not content:
        return "RED", "gate.yml not found", "gh-api:repo-contents"
    import base64
    body = base64.b64decode(content.get("content", "") or "").decode(errors="replace")
    has_push = "push:" in body or "'push'" in body or '"push"' in body
    return green_if(has_push), f"gate.yml {'auto-triggers on push' if has_push else 'manual-only'} — enforcement proxy", "gh-api:repo-contents"


CI_CHECKS = {
    "verification": c_verification,
    "monitoring": c_monitoring,
    "data_quality": c_data_quality,
    "performance": c_performance,
    "infrastructure": c_infrastructure,
    "configuration": c_configuration,
    "discoverability": c_discoverability,
    "documentation": c_documentation,
    "security": c_security,
    "change_mgmt": c_change_mgmt,
}


def verify():
    aspects = {}
    for name, fn in CI_CHECKS.items():
        try:
            result = fn()
            # normalize: function returns (status, evidence[, source])
            status = result[0]
            evidence = result[1]
            source = result[2] if len(result) > 2 else "gh-api"
            aspects[name] = {"status": status, "evidence": evidence, "source": source}
        except Exception as e:
            aspects[name] = {"status": "RED", "evidence": f"check error: {e}", "source": "error"}
    return {"module": "INF", "generated_at": NOW.isoformat(), "aspects": aspects}


if __name__ == "__main__":
    result = verify()
    json.dump(result, sys.stdout, indent=2)
    counts = {}
    for v in result["aspects"].values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"\nINF Aspect Status Summary: {counts}", file=sys.stderr)
