#!/usr/bin/env python3
"""
SUP Aspect Status (verified) — CI-runnable verifier for the Quality Matrix.

Runs objective machine checks on CI-accessible signals (gh API, R2 proxy).
Workspace-dependent aspects (data_quality, configuration, discoverability,
documentation, change_mgmt) are carried forward from the last manual run.

This mirrors the proven TAG/AGG/OUT pattern: publish signal-based aspects
in CI, carry forward workspace aspects from the last full manual run.

INF-ASPECT-CADENCE-1: wires SUP's aspect-status to a 6h schedule.
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


# --- Workspace-dependent aspects (carry forward from last manual run) ---
CARRY_FORWARD = [
    "data_quality", "configuration", "discoverability",
    "documentation", "change_mgmt",
]


def get_previous():
    """Fetch previous aspect-status from proxy for carry-forward."""
    return proxy_json("sup-aspect-status.json") or {}


CI_CHECKS = {
    "verification": c_verification,
    "monitoring": c_monitoring,
    "security": c_security,
}


def verify():
    aspects = {}
    prev = get_previous()
    prev_aspects = prev.get("aspects", {})
    prev_date = prev.get("generated_at", "?")[:10]

    # Run CI-runnable checks
    for name, func in CI_CHECKS.items():
        try:
            status, evidence, source = func()
        except Exception as e:
            status, evidence, source = "YELLOW", f"check error: {e}", "error"
        aspects[name] = {"status": status, "evidence": evidence, "source": source}

    # Carry forward workspace-dependent aspects
    for name in CARRY_FORWARD:
        if name in prev_aspects:
            old = prev_aspects[name]
            aspects[name] = {
                "status": old.get("status", "YELLOW"),
                "evidence": f"(carry-forward {prev_date}) {old.get('evidence', '')}",
                "source": old.get("source", "carry-forward"),
            }
        else:
            aspects[name] = {
                "status": "YELLOW",
                "evidence": f"(carry-forward) no previous data for {name} — manual run needed",
                "source": "carry-forward:missing",
            }

    return {"module": "SUP", "generated_at": NOW.isoformat(), "aspects": aspects}


if __name__ == "__main__":
    result = verify()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")

    counts = {}
    for k, v in result["aspects"].items():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"\nSUP Aspect Status Summary: {counts}", file=sys.stderr)
