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
WORKERS_DEV_URL = "https://zjp-dashboard.wild-queen-069e.workers.dev/"  # must stay pinned OFF (wrangler workers_dev:false)


GH_LAST_ERR = [None]


def gh_json(args, attempts=3, token_env=None):
    """gh api helper. token_env: name of an env var holding a DEDICATED token
    (e.g. DASH_REPO_TOKEN/GH_PAT_DASH) — when set+non-empty it overrides GH_TOKEN
    for this call only, so a repo-scoped PAT can read zjp-dashboard without
    replacing the org-wide GH_PAT used by every other call."""
    env = os.environ
    if token_env:
        dedicated = (os.environ.get(token_env) or "").strip()
        if dedicated:
            env = {**os.environ, "GH_TOKEN": dedicated}
    for a in range(attempts):
        try:
            result = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=30, env=env)
            if result.returncode == 0 and result.stdout.strip():
                GH_LAST_ERR[0] = None
                return json.loads(result.stdout)
            GH_LAST_ERR[0] = (result.stderr or result.stdout or "empty output").strip().splitlines()[0][:160]
            if a < attempts - 1:
                time.sleep(2)
        except Exception as e:
            GH_LAST_ERR[0] = str(e)[:160]
            if a < attempts - 1:
                time.sleep(2)
    return None


def head_check_runs():
    """check-runs for the HEAD commit of zjp-dashboard main: {name: run}."""
    data = gh_json(["api", f"repos/{DASH_REPO}/commits/main/check-runs",
                    "--jq", "{runs: [.check_runs[] | {name: .name, conclusion: .conclusion, status: .status}]}"],
                   token_env="DASH_REPO_TOKEN")
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


# ─── Pure decision logic (no I/O) — unit-tested in verify-dash-aspect-status.test.py ───

def classify_check(run, label):
    """Check-run ladder shared by verification + infrastructure:
    missing/not-completed = YELLOW transient (fresh push / in-flight), completed
    failure = RED, completed success = GREEN."""
    if run is None:
        return "YELLOW", f"no '{label}' check on main HEAD"
    if run.get("status") != "completed":
        return "YELLOW", f"{label} {run.get('status')} on main HEAD"
    return ("GREEN" if run.get("conclusion") == "success" else "RED"), \
           f"{label} {run.get('conclusion')} on main HEAD"


def classify_unauth(http_code):
    """Unauth /api/data probe: 200 = exposure (RED); 3xx/4xx/5xx = did not
    serve the payload unauthenticated (YELLOW — the PAT-gated remainder)."""
    return "RED" if http_code == 200 else "YELLOW"


def divergence_band(served, r2_pool):
    """Served-vs-R2-canonical pool divergence: (band, pct).
    'agree' <0.5% (normal cycle lag), 'lagging' <5%, 'broken' >=5% (read-path
    stale/broken, Aug-3 incident family). Band None when not computable."""
    if served is None or not r2_pool:
        return None, None
    pct = abs(served - r2_pool) / r2_pool * 100
    return ("agree" if pct < 0.5 else ("lagging" if pct < 5 else "broken")), pct


def freshness_band(age_min):
    """LIVE <30m / DELAYED <6h / DOWN >=6h — mirrors the dashboard's own model."""
    if age_min < 30: return "GREEN"
    if age_min < 360: return "YELLOW"
    return "RED"


def classify_subdomain_probe(code):
    """workers.dev route state from a live probe: 200 = the app is SERVING on the
    ungated subdomain (RED — the G24 backdoor class for an Access-gated app);
    404/3xx/5xx/None (edge error or DNS fail) = route not serving = pinned."""
    return "RED" if code == 200 else "GREEN"


def classify_dependabot(alerts, err):
    """(status, note) for Dependabot posture: 0 open = GREEN; open alerts or a
    disabled/unreadable API = YELLOW-with-ask (never invented green)."""
    if err is not None:
        if "disabled" in err.lower():
            return "YELLOW", "Dependabot DISABLED on zjp-dashboard — enable it (INF hardening P2) or accept the posture explicitly"
        if "not accessible" in err.lower() or "not found" in err.lower():
            return "YELLOW", "Dependabot unreadable — GH_PAT_DASH (DASH_REPO_TOKEN) lacks zjp-dashboard access (INF-GHPAT-ZJPDASH-SCOPE-1)"
        return "YELLOW", f"Dependabot unreadable ({err[:90]})"
    n = len(alerts)
    if n == 0:
        return "GREEN", "0 open Dependabot alerts"
    return "YELLOW", f"{n} open Dependabot alerts — triage"



def latency_band(elapsed_s):
    """<5s GREEN / <15s YELLOW (the dashboard's own fetchWithTimeout budget) / else RED."""
    if elapsed_s < 5: return "GREEN"
    if elapsed_s < 15: return "YELLOW"
    return "RED"

def c_verification():
    runs = head_check_runs()
    if runs is None:
        return "YELLOW", f"gh-api check-runs unreadable: {GH_LAST_ERR[0] or 'unknown error'}", "gh-api:check-runs"
    status, summary = classify_check(runs.get("verify"), "verify")
    return status, summary, "gh-api:check-runs"


def c_infrastructure():
    """Deploy currency: Workers Builds check success on HEAD + edge answers."""
    runs = head_check_runs()
    wb = (runs or {}).get("Workers Builds: zjp-dashboard")
    edge = None
    try:
        req = urllib.request.Request(DASH_URL + "/", headers={"User-Agent": "ZJP-DashAspectVerifier/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # 302 counts (manual not needed; urllib errors on redirect — probe follow-up below)
            edge = resp.status
    except urllib.error.HTTPError as e:
        edge = e.code  # 3xx/4xx from CF Access = edge alive
    except Exception:
        edge = None
    if runs is None:
        # Credential invisibility (PAT repo list) is NOT a deploy failure — RED here would
        # page Discord via check-40 for a non-incident. YELLOW carries the operator ask.
        return "YELLOW", f"check-runs unreadable ({GH_LAST_ERR[0] or 'unknown'}) — GH_PAT cannot see zjp-dashboard; add the repo to the PAT access list (INF-GHPAT-ZJPDASH-SCOPE-1); edge {edge}", "gh-api:check-runs + edge probe"
    if wb is None:
        # Fresh push: the Workers Builds check-run may not exist yet. Not a failure.
        return "YELLOW", f"Workers Builds check not created yet on main HEAD (fresh push?); edge {edge}", "gh-api:check-runs + edge probe"
    status, summary = classify_check(wb, "Workers Builds")
    if status != "GREEN":
        return status, f"{summary}; edge {edge}", "gh-api:check-runs + edge probe"
    if edge is None or edge >= 500:
        return "RED", f"Workers Builds success but edge unreachable ({edge})", "gh-api:check-runs + edge probe"
    return "GREEN", f"{summary}; edge HTTP {edge}", "gh-api:check-runs + edge probe"


def proxy_json(path):
    try:
        req = urllib.request.Request(f"{PROXY}/{path}", headers={
            "User-Agent": "ZJP-DashAspectVerifier/1.0",
            "X-Proxy-Token": os.environ.get("DATA_PROXY_TOKEN", "").strip()})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[proxy_json] {path}: {e}", file=sys.stderr)
        return None


def c_security():
    """Three exposure/posture checks:
    (a) unauthenticated /api/data must be Access-blocked (service-role-key routes,
        token dispatch behind this endpoint);
    (b) the workers.dev route must stay pinned OFF (the G24 ungated-backdoor class)
        — verifiable PAT-free by probing the subdomain (not serving = pinned);
    (c) Dependabot open alerts on zjp-dashboard — needs DASH_REPO_TOKEN/GH_PAT_DASH
        with the repo in its access list (INF-GHPAT-ZJPDASH-SCOPE-1); if Dependabot
        is disabled on the repo, that fact is surfaced (operator hardening-P2 call).
    GREEN only when all three are actually verified healthy — no invented green."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # treat 3xx as terminal (check-43's redirect:'manual' pattern)
    facts, exposures = [], []
    # (a) unauth probe
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(f"{DASH_URL}/api/data", headers={"User-Agent": "ZJP-DashAspectVerifier/1.0"})
        opener.open(req, timeout=20)
        unauth = 200  # got a full response WITHOUT Access credentials — exposure
    except urllib.error.HTTPError as e:
        unauth = e.code  # 3xx (Access redirect) or 401/403 = denied — expected
    except Exception as e:
        return "YELLOW", f"unauth probe failed to resolve ({e})", "dash:/api/data + workers.dev probes"
    if classify_unauth(unauth) == "RED":
        exposures.append("UNAUTHENTICATED /api/data returned 200 — Cloudflare Access is not gating the API surface")
    else:
        facts.append(f"unauth denied (HTTP {unauth})")
    # (b) workers.dev pin — PAT-free live probe
    wd_code = None
    try:
        req = urllib.request.Request(WORKERS_DEV_URL, headers={"User-Agent": "ZJP-DashAspectVerifier/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            wd_code = resp.status
    except urllib.error.HTTPError as e:
        wd_code = e.code  # 404 from the edge = workers.dev disabled for this Worker
    except Exception:
        wd_code = None  # DNS/connection failure = route not serving = pinned
    if classify_subdomain_probe(wd_code) == "RED":
        exposures.append(f"workers.dev route SERVING (HTTP {wd_code}) — the ungated-backdoor class; pin workers_dev=false")
    else:
        facts.append(f"workers.dev pinned (probe HTTP {wd_code})")
    # (c) Dependabot
    db_status, db_note = dependabot_state()
    if exposures:
        return "RED", "; ".join(exposures), "dash:/api/data + workers.dev probes + gh-api:dependabot"
    if db_status == "GREEN":
        facts.append(db_note)
        return "GREEN", f"{'; '.join(facts)}; {db_note}", "dash:/api/data + workers.dev probes + gh-api:dependabot"
    return "YELLOW", f"{'; '.join(facts)}; {db_note}", "dash:/api/data + workers.dev probes + gh-api:dependabot"


def dependabot_state():
    """(status, note) for Dependabot on zjp-dashboard, via the dedicated repo
    token when present. Pure classification lives in classify_dependabot; this
    does the I/O."""
    data = gh_json(["api", f"repos/{DASH_REPO}/dependabot/alerts?state=open",
                    "--jq", "[.[] | {severity: .security_advisory.severity, url: .html_url}]"],
                   token_env="DASH_REPO_TOKEN")
    if data is None:
        err = (GH_LAST_ERR[0] or "unreadable")
        return classify_dependabot(None, err)
    return classify_dependabot(data, None)


def c_data_quality():
    """Payload completeness + freshness through the dashboard's own read path,
    PLUS an external cross-check: the served payload vs R2-canonical zjp-metrics.
    The completeness field alone is circular (computed by the API it grades); the
    cross-check catches the stale-Supabase-row class (Aug-3 incident family)."""
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
    # External cross-check: R2-canonical zjp-metrics vs what the dashboard serves.
    # Small divergence is NORMAL propagation lag (Supabase publish trails R2 by ~a cycle;
    # pool moves every cycle); bands: <0.5% fine, <5% lagging, >=5% read path broken/stale.
    served_pool = ((zjp.get("pool") or {}).get("total_jobs"))
    r2 = proxy_json("zjp-metrics.json")
    r2_pool = ((r2 or {}).get("pool") or {}).get("total_jobs")
    cross = ""
    band, divergence = divergence_band(served_pool, r2_pool)
    if r2 is None:
        cross = "; R2 cross-check unreadable (proxy)"
    elif band == "agree":
        cross = f"; R2 agrees (pool {r2_pool:,}, diff {divergence:.2f}%)"
    elif band == "lagging":
        cross = f"; R2 lagging: served {served_pool:,} vs R2 {r2_pool:,} ({divergence:.1f}%)"
    elif band == "broken":
        problems.append(f"read-path divergence: served pool {served_pool:,} != R2 {r2_pool:,} ({divergence:.1f}%)")
    elif served_pool is not None and r2_pool is not None and served_pool != r2_pool:
        problems.append(f"read-path divergence: served pool {served_pool:,} != R2 {r2_pool:,}")
    if problems:
        return "RED", "; ".join(problems) + cross, "dash:/api/data + data-proxy:zjp-metrics"
    # Freshness bands mirror the dashboard's own model: LIVE <30m, DELAYED <6h, DOWN >=6h.
    status = freshness_band(age_min)
    return status, f"/api/data complete; zjpMetrics age {age_min:.0f}m{cross}", "dash:/api/data + data-proxy:zjp-metrics"


def c_performance():
    elapsed, code, _, note = fetch_api_data()
    if code is None and note and "not configured" in note:
        return "YELLOW", note + " — latency probe skipped", "dash:/api/data"
    if code != 200:
        return "RED", f"/api/data unreachable ({note})", "dash:/api/data"
    # Budget mirrors the dashboard's own fetchWithTimeout (15s): <5s GREEN, <15s YELLOW.
    status = latency_band(elapsed)
    return status, f"/api/data (the dashboard's data-fetch leg — the one combined API call every page awaits) responded in {elapsed:.1f}s", "dash:/api/data"

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
    "security": c_security,
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
    counts = {}
    for k, v in result["aspects"].items():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"\nDASH Aspect Status Summary: {counts}", file=sys.stderr)

    if do_publish:
        try:
            publish_r2(data_str)
        except Exception as e:
            print(f"R2 publish FAILED: {e}", file=sys.stderr)
