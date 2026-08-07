#!/usr/bin/env python3
"""
verify-out-aspect-status.py — OUT aspect-status VERIFIER (ASPECT_STATUS_CONTRACT.md, verified model).

Produces OUT's aspect-status as the RESULT of objective machine checks on live signals
(NOT self-report). CI-portable: uses repo-relative paths (.github/scripts/processing submodule),
gh-api (GH_TOKEN), and the zjp-data-proxy. Run from the jobs-data-2026 repo root.

Aspects (OUT-scoped, per OUT-ASPECT-VERIFY-1):
  verification, monitoring, performance, infrastructure, configuration, discoverability,
  security, documentation, change_mgmt, data_quality -> objective checks
  content_quality, coordination -> OMITTED (subjective -> STATE narrative)

v1 caveats (honest): data_quality reads the dead-link scan (known to undercount soft-404/IP-
dependent dead links — TikTok; real rate higher than reported); configuration/discoverability/
change_mgmt are structural-existence proxies (not deep validity); verification is a 3-run snapshot.
"""
import json, os, subprocess, urllib.request, datetime, sys, time

NOW = datetime.datetime.now(datetime.timezone.utc)
PROXY = "https://zjp-data-proxy.wild-queen-069e.workers.dev/data"
REPOS = ["zapplyjobs/jobs-data-2026", "zapplyjobs/job-board-processing"]
PROC = ".github/scripts/processing/lib/src"  # processing submodule (job-board-processing)

def gh_json(args, attempts=3):
    # Retry on transient gh-api failures (rate-limit/network) so a flaky call doesn't produce a
    # false aspect status. Legit empty output still returns None (rare for these endpoints).
    for a in range(attempts):
        try:
            out = subprocess.check_output(["gh"] + args, text=True, stderr=subprocess.DEVNULL, timeout=30)
            return json.loads(out) if out.strip() else None
        except Exception:
            if a < attempts - 1:
                time.sleep(2 * (a + 1))
    return None

def gh_runs(repo, workflow=None, limit=5):
    args = ["run", "list", "-R", repo, "-L", str(limit), "--json", "status,conclusion,createdAt,updatedAt"]
    if workflow:
        args[1:1] = ["-w", workflow]
    return [r for r in (gh_json(args) or []) if r.get("status") == "completed"]

def proxy_json(path):
    try:
        req = urllib.request.Request(f"{PROXY}/{path}", headers={"User-Agent": "out-aspect-verifier/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def green_if(cond): return "GREEN" if cond else "RED"

def c_verification():
    # Workflows that SHOULD pass. out-health-check + out-dead-link-check are BOTH EXCLUDED —
    # their exit-1 is BY DESIGN (issues found -> tracking issue), not a CI failure.
    # Health-check detects data quality issues (visa fill, Undated %); dead-link-check
    # detects dead URLs. Both correctly exit 1 when they find issues. The monitoring aspect
    # (c_monitoring) checks they're CONFIGURED + have recent runs — that's the right gate.
    fails = total = 0; detail = []
    for wf in ("post-to-discord.yml",):
        runs = gh_runs("zapplyjobs/jobs-data-2026", workflow=wf, limit=3)
        for r in runs:
            total += 1
            if r.get("conclusion") != "success":
                fails += 1
        detail.append(f"{wf}:{len(runs)}")
    status = "GREEN" if (total and fails == 0) else ("YELLOW" if fails < total else "RED")
    return status, f"{total} recent runs, {fails} non-success ({', '.join(detail)})", "gh-api:CI"

def c_monitoring():
    hc = gh_runs("zapplyjobs/jobs-data-2026", workflow="out-health-check.yml", limit=1)
    dl = gh_runs("zapplyjobs/jobs-data-2026", workflow="out-dead-link-check.yml", limit=1)
    return green_if(hc and dl), "health-check + dead-link-scan configured, recent runs present", "gh-api:workflows"

def c_performance():
    # limit=5 + most-recent COMPLETED run — avoids a false "no recent run" RED when the latest
    # run is momentarily in-progress (post-to-discord runs every 15min; ~13% in-progress window).
    runs = gh_runs("zapplyjobs/jobs-data-2026", workflow="post-to-discord.yml", limit=5)
    if not runs:
        return "RED", "no recent COMPLETED post-to-discord run (of last 5)", "gh-api:runtime"
    try:
        c = datetime.datetime.fromisoformat(runs[0]["createdAt"].replace("Z", "+00:00"))
        u = datetime.datetime.fromisoformat(runs[0]["updatedAt"].replace("Z", "+00:00"))
        secs = (u - c).total_seconds()
        status = "GREEN" if secs < 900 else ("YELLOW" if secs < 1500 else "RED")
        return status, f"post-to-discord run {secs:.0f}s", "gh-api:runtime"
    except Exception:
        return "YELLOW", "runtime parse failed", "gh-api:runtime"

def c_infrastructure():
    hc = proxy_json("out-health-check.json")
    if not hc:
        return "RED", "out-health-check.json unreachable", "proxy:out-health-check"
    dests = hc.get("destinations") or hc.get("healthCheck", {}).get("destinations") or []
    repos = hc.get("repos") or []
    # Gate on BOTH destination uptime (sjd/zapply boards) AND consumer-repo push health
    # (README deploy verdicts). Previously only destinations were gated — a stale consumer
    # repo left this aspect GREEN even though OUT output stopped reaching that surface.
    failing = ([d.get("name") for d in dests if (d.get("verdict", "").upper() != "PASS")]
               + [r.get("repo") for r in repos if (r.get("verdict", "").upper() != "PASS")])
    if not dests and not repos:
        return "YELLOW", "no destinations/repos in health-check", "proxy:out-health-check"
    return (green_if(not failing),
            f"{len(dests)} destinations + {len(repos)} consumer repos, {len(failing)} failing",
            "proxy:out-health-check")

def c_configuration():
    paths = [f"{PROC}/discord/config.js", f"{PROC}/board-types.js"]
    missing = [p for p in paths if not os.path.exists(p)]
    return green_if(not missing), f"{len(paths)-len(missing)}/{len(paths)} OUT config files present (existence proxy)", "fs:config"

def c_discoverability():
    dirs = ["routing", "discord", "data"]
    found = [d for d in dirs if os.path.isdir(f"{PROC}/{d}")]
    return green_if(len(found) == len(dirs)), f"processing lib structure {len(found)}/{len(dirs)} present (structural proxy)", "fs:lib-structure"

def c_security():
    crit = opens = 0; failed = []
    for repo in REPOS:
        alerts = gh_json(["api", f"repos/{repo}/dependabot/alerts", "-q", '[.[] | select(.state=="open")]'])
        if alerts is None:
            failed.append(repo); continue   # api failure — don't false-GREEN ("0 alerts")
        opens += len(alerts)
        crit += sum(1 for a in alerts if (a.get("security_advisory", {}) or {}).get("severity") in ("critical", "high"))
    if failed:
        return "YELLOW", f"unable to verify {failed} (dependabot api); {opens} open / {crit} critical in the rest", "gh-api:dependabot"
    return green_if(crit == 0), f"{opens} open alerts, {crit} critical/high", "gh-api:dependabot"

def c_documentation():
    try:
        out = subprocess.check_output(["git", "log", "-1", "--format=%ct"], text=True, timeout=10).strip()
        age_d = (NOW.timestamp() - float(out)) / 86400
        status = "GREEN" if age_d < 3 else ("YELLOW" if age_d < 14 else "RED")
        return status, f"repo last commit {age_d:.1f}d ago (activity/freshness proxy)", "git:repo-commits"
    except Exception:
        return "YELLOW", "git log failed", "git:repo-commits"

def c_change_mgmt():
    wfdir = ".github/workflows"
    try:
        files = os.listdir(wfdir) if os.path.isdir(wfdir) else []
    except Exception:
        files = []
    has_gate = any("gate" in f.lower() or "ci-gate" in f.lower() for f in files)
    return green_if(has_gate), f"CI-gate workflow present ({has_gate}) — SDLC structural proxy", "fs:workflows"

def c_data_quality():
    issues = []
    # 1. Dead-link rate (existing check)
    dl = proxy_json("dead-links.json")
    if not dl:
        issues.append(("warn", "dead-links.json unreachable"))
    else:
        checked = dl.get("total_checked", 0); dead = dl.get("total_dead", 0)
        rate = dead / checked if checked else 1
        if rate >= 0.05: issues.append(("red", f"dead-link {rate*100:.1f}% ({dead}/{checked})"))
        elif rate >= 0.01: issues.append(("warn", f"dead-link {rate*100:.1f}% ({dead}/{checked})"))
    # 2. Visa fill + undated rate from health check (catches enrichment failures across sampled repos)
    hc = proxy_json("out-health-check.json")
    if hc:
        for r in (hc.get("repos") or []):
            visa = r.get("readme_visa_pct")
            undated = r.get("readme_undated_pct")
            if visa is not None:
                repo_name = r.get("repo", "?")
                if visa < 10: issues.append(("red", f"{repo_name} visa {visa}% (<10%)"))
                if undated is not None and undated > 80: issues.append(("red", f"{repo_name} undated {undated}% (>80%)"))
    reds = [m for s, m in issues if s == "red"]
    warns = [m for s, m in issues if s == "warn"]
    if reds: status = "RED"
    elif warns: status = "YELLOW"
    else: status = "GREEN"
    evidence = "; ".join(m for _, m in issues) if issues else "dead-link + visa + undated all healthy"
    return status, f"{evidence}", "proxy:dead-links+health-check"

CHECKS = {
    "verification": c_verification, "monitoring": c_monitoring, "performance": c_performance,
    "infrastructure": c_infrastructure, "configuration": c_configuration,
    "discoverability": c_discoverability, "security": c_security, "documentation": c_documentation,
    "change_mgmt": c_change_mgmt, "data_quality": c_data_quality,
}

def verify():
    aspects = {}
    for name, fn in CHECKS.items():
        try:
            status, evidence, source = fn()
        except Exception as e:
            status, evidence, source = "RED", f"check error: {e}", "verifier-error"
        aspects[name] = {"status": status, "evidence": evidence, "source": source}
    return {"module": "OUT", "generated_at": NOW.isoformat(), "aspects": aspects}

if __name__ == "__main__":
    result = verify()
    print(json.dumps(result, indent=2))
    counts = {}
    for a in result["aspects"].values():
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    print(f"\nSummary: {counts}", file=sys.stderr)
