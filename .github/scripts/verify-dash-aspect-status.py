#!/usr/bin/env python3
"""
DASH Aspect Status (verified) — CI-native verifier for the Quality Matrix.

Closes the "who watches the watchman" gap: the dashboard displays every module's
health but had no row of its own, and the only external watch (check-43) is
availability-only — blind to the real DASH incident classes (false-healthy
payloads, silent deploy-stale Workers, red CI). This verifier runs OUTSIDE the
dashboard (jobs-data-2026 CI), so the row it publishes is non-circular.

All aspects are live-checked: gh-api check-runs (CI + Workers Builds deploy),
authenticated /api/data probe (Access service token), edge availability, and the
check-43 watchdog heartbeat via the data proxy. No carry-forward.

Aspect bands mirror the dashboard's own freshness model (LIVE <30m / DELAYED <6h
/ DOWN >=6h) and its fetch budget (fetchWithTimeout 15s).
"""
import json, os, subprocess, urllib.request, datetime, sys, time

NOW = datetime.datetime.now(datetime.timezone.utc)
DASH_REPO = "zapplyjobs/zjp-dashboard"
DASH_URL = "https://dash.zapply.jobs"
PROXY = "https://zjp-data-proxy.wild-queen-069e.workers.dev/data"


def gh_json(args, attempts=3):
    for a in range(attempts):
        try:
            result = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
            if a < attempts:
                time.sleep(2)
        except Exception:
            if a < attempts - 1:
                time.sleep(2)
    return None


def head_check_runs():
    """check-runs for the HEAD commit of zjp-dashboard main: {name: conclusion}."""
    data = gh_json(["api", f"repos/{DASH_REPO}/commits/main/check-runs",
                    "--jq", "{runs: [.check_runs[] | {name: .name, conclusion: .conclusion, status: .status}]}"])
    if not data or "runs" not in data:
        return None
    return {r["name"]: r for r in data["runs"]}


def fetch_api_data():
    """Authenticated /api/data probe via the CF Access service token.
    Returns (seconds, status_code, payload_or_None, note)."""
    cid = (os.environ.get("DASH_ACCESS_CLIENT_ID") or "").strip()
    secret = (os.environ.get("DASH_ACCESS_CLIENT_SECRET") or "").strip()
    if not cid or not secret:
        return None, None, None, "Access service token not configured (secrets DASH_ACCESS_CLIENT_ID/SECRET)"
    req = urllib.request.Request(f"{DASH_URL}/api/data", headers={
        "User-Agent": "ZJP-DashAspectVerifier/1.0",
        "CF-Access-Client-Id": cid,
        "CF-Access-Client-Secret": secret,
    })
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            elapsed = time.monotonic() - t0
            return elapsed, resp.status, json.loads(body), None
    except Exception as e:
        return time.monotonic() - t0, None, None, str(e)


def c_verification():
    runs = head_check_runs()
    if runs is None:
        return "YELLOW", "gh-api check-runs unreadable (rate limit?)", "gh-api:check-runs"
    v = runs.get("verify")
    if not v:
        return "YELLOW", "no 'verify' check on main HEAD", "gh-api:check-runs"
    if v.get("status") != "completed":
        return "YELLOW", f"verify {v.get('status')} on main HEAD", "gh-api:check-runs"
    return ("GREEN" if v.get("conclusion") == "success" else "RED"), \
           f"verify {v.get('conclusion')} on main HEAD", "gh-api:check-runs"


def c_infrastructure():
    """Deploy currency: Workers Builds check success on HEAD + edge answers."""
    runs = head_check_runs()
    wb = (runs or {}).get("Workers Builds: zjp-dashboard")
    deploy = None if runs is None else (wb or {}).get("conclusion")
    edge = None
    try:
        req = urllib.request.Request(DASH_URL + "/", headers={"User-Agent": "ZJP-DashAspectVerifier/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # 302 counts (manual not needed; urllib errors on redirect — probe follow-up below)
            edge = resp.status
    except urllib.error.HTTPError as e:
        edge = e.code  # 3xx/4xx from CF Access = edge alive
    except Exception:
        edge = None
    if deploy != "success":
        return "RED", f"Workers Builds {deploy or 'unknown'} on main HEAD; edge {edge}", "gh-api:check-runs + edge probe"
    if edge is None or edge >= 500:
        return "RED", f"Workers Builds success but edge unreachable ({edge})", "gh-api:check-runs + edge probe"
    return "GREEN", f"Workers Builds success on main HEAD; edge HTTP {edge}", "gh-api:check-runs + edge probe"


def c_data_quality():
    """Payload completeness + freshness through the dashboard's own read path.
    Catches the false-healthy class (200 + nulls / stale-but-fresh)."""
    elapsed, code, payload, note = fetch_api_data()
    if code is None and note and "not configured" in note:
        return "YELLOW", note + " — live completeness probe skipped", "dash:/api/data"
    if code != 200 or not isinstance(payload, dict):
        return "RED", f"/api/data HTTP {code} ({note})", "dash:/api/data"
    missing = payload.get("_missingDataKeys")
    zjp = payload.get("zjpMetrics") or {}
    gen = zjp.get("generated_at")
    nulls = [k for k, v in payload.items() if v is None]
    problems = []
    if missing: problems.append(f"{len(missing)} missing keys")
    if nulls: problems.append(f"null top-levels: {','.join(nulls[:4])}")
    if not gen: problems.append("no zjpMetrics.generated_at")
    age_min = None
    if gen:
        try:
            age_min = (NOW - datetime.datetime.fromisoformat(gen.replace("Z", "+00:00"))).total_seconds() / 60
        except Exception:
            problems.append("generated_at unparseable")
    if problems:
        return "RED", "; ".join(problems), "dash:/api/data"
    # Freshness bands mirror the dashboard's own model: LIVE <30m, DELAYED <6h, DOWN >=6h.
    if age_min < 30: status = "GREEN"
    elif age_min < 360: status = "YELLOW"
    else: status = "RED"
    return status, f"/api/data complete; zjpMetrics age {age_min:.0f}m", "dash:/api/data"


def c_performance():
    elapsed, code, _, note = fetch_api_data()
    if code is None and note and "not configured" in note:
        return "YELLOW", note + " — latency probe skipped", "dash:/api/data"
    if code != 200:
        return "RED", f"/api/data unreachable ({note})", "dash:/api/data"
    # Budget mirrors the dashboard's own fetchWithTimeout (15s): <5s GREEN, <15s YELLOW.
    status = "GREEN" if elapsed < 5 else ("YELLOW" if elapsed < 15 else "RED")
    return status, f"/api/data responded in {elapsed:.1f}s", "dash:/api/data"


def c_monitoring():
    """The dashboard's own watchdog (check-43) is configured (source file on main)
    and the alert cycle is heartbeating (history fresh). History records carry
    failed_ids/warned_ids only — check-43 appears there only when it FAILS, so its
    absence proves nothing; configuration is verified against the deployed source."""
    problems = []
    # (a) configured: check-43 exists in the deployed pipeline source (job-board-processing main)
    res = gh_json(["api", "repos/zapplyjobs/job-board-processing/contents/"
                          "lib/checks/check-43-dashboard-uptime.js"])
    configured = bool(res and res.get("sha"))
    if not configured:
        problems.append("check-43 source not found on job-board-processing main")
    # (b) heartbeating: latest alert-history snapshot is fresh (< 30 min)
    last_age_min = None
    try:
        url = f"{PROXY}/pipeline-alert-history.jsonl"
        req = urllib.request.Request(url, headers={
            "User-Agent": "ZJP-DashAspectVerifier/1.0",
            "X-Proxy-Token": os.environ.get("DATA_PROXY_TOKEN", "").strip()})
        with urllib.request.urlopen(req, timeout=15) as resp:
            lines = [l for l in resp.read().decode().splitlines() if l.strip()]
        rec = json.loads(lines[-1]) if lines else {}
        checked = rec.get("checked_at")
        if checked:
            last_age_min = (NOW - datetime.datetime.fromisoformat(checked.replace("Z", "+00:00"))).total_seconds() / 60
        else:
            problems.append("history records carry no checked_at")
    except Exception as e:
        problems.append(f"alert-history unreadable ({e})")
    if last_age_min is not None and last_age_min > 30:
        problems.append(f"alert cycle last ran {last_age_min:.0f}m ago")
    if problems:
        return "YELLOW", "; ".join(problems), "gh-api:contents + data-proxy:pipeline-alert-history"
    return "GREEN", f"check-43 configured; alert cycle {last_age_min:.0f}m ago", \
           "gh-api:contents + data-proxy:pipeline-alert-history"


CHECKS = {
    "verification": c_verification,
    "infrastructure": c_infrastructure,
    "data_quality": c_data_quality,
    "performance": c_performance,
    "monitoring": c_monitoring,
}


def verify():
    aspects = {}
    for name, func in CHECKS.items():
        try:
            status, evidence, source = func()
        except Exception as e:
            status, evidence, source = "YELLOW", f"check error: {e}", "error"
        aspects[name] = {"status": status, "evidence": evidence, "source": source}
    return {"module": "DASH", "generated_at": NOW.isoformat(), "aspects": aspects}


def publish_r2(data_str):
    """R2-PRIMARY: publish dash-aspect-status.json to R2 (canonical object store).
    Uses boto3 (proper SigV4). Reads R2_* env. Mirrors the SUP verifier."""
    import boto3
    s3 = boto3.client("s3", region_name="auto",
                      endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    s3.put_object(Bucket=os.environ["R2_BUCKET_NAME"],
                  Key="data/dash-aspect-status.json",
                  Body=data_str, ContentType="application/json")
    print("published R2: data/dash-aspect-status.json", file=sys.stderr)


if __name__ == "__main__":
    do_publish = "--publish" in sys.argv
    result = verify()
    data_str = json.dumps(result, indent=2)
    sys.stdout.write(data_str + "\n")

    counts = {}
    for k, v in result["aspects"].items():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"\nDASH Aspect Status Summary: {counts}", file=sys.stderr)

    if do_publish:
        try:
            publish_r2(data_str)
        except Exception as e:
            print(f"R2 publish FAILED: {e}", file=sys.stderr)
