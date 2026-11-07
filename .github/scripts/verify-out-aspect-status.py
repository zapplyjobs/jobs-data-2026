#!/usr/bin/env python3
# CANONICAL: this jobs-data-2026 copy publishes the official artifact (pipeline-data/out-aspect-status.json). A session-side mirror exists at GenAI_Work projects/zjp/scripts/ and DRIFTS - never treat the mirror as truth (OUT-ASPECTVERIFIER-MIRROR-DRIFT-1, false RED 09-12).
"""
verify-out-aspect-status.py — OUT aspect-status VERIFIER (ASPECT_STATUS_CONTRACT.md, verified model).

Produces OUT's aspect-status as the RESULT of objective machine checks on live signals
(NOT self-report). CI-portable: uses repo-relative paths (.github/scripts/processing submodule),
gh-api (GH_TOKEN), and the zjp-data-proxy. Run from the jobs-data-2026 repo root.

Aspects (OUT-scoped, per OUT-ASPECT-VERIFY-1):
  verification, monitoring, performance, infrastructure, configuration, discoverability,
  security, documentation, change_mgmt, data_quality -> objective checks
  readme_truth, liveness_canary, semantic_conformance -> objective checks (CI-ported riders:
    readme_truth + liveness_canary 2026-11-05; semantic_conformance 2026-11-06 = gated legs 1+2
    of the session deep-pass monitor, RED on WORSENING not presence - legs 3/4 + disposals stay
    session-side)
  content_quality, coordination -> OMITTED (subjective -> STATE narrative)

v1 caveats (honest): data_quality reads the dead-link scan (known to undercount soft-404/IP-
dependent dead links — TikTok; real rate higher than reported); configuration/discoverability/
change_mgmt are structural-existence proxies (not deep validity); verification is a 3-run snapshot.
"""
import base64, json, os, re, subprocess, urllib.request, urllib.parse, datetime, sys, time
from concurrent.futures import ThreadPoolExecutor

NOW = datetime.datetime.now(datetime.timezone.utc)
PROXY = "https://zjp-data-proxy.wild-queen-069e.workers.dev/data"
REPOS = ["zapplyjobs/jobs-data-2026", "zapplyjobs/job-board-processing"]
PROC = ".github/scripts/processing/lib/src"  # processing submodule (job-board-processing)
ORG = "zapplyjobs"
REGISTRY = ".github/scripts/processing/lib/board-registry.json"  # membership single source (OUT-BOARDLIST-SINGLE-SOURCE-1)

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
        req = urllib.request.Request(f"{PROXY}/{path}", headers={"User-Agent": "out-aspect-verifier/1.0", "X-Proxy-Token": os.environ.get("DATA_PROXY_TOKEN", "")})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def guard_artifact():
    """sjd-liveness-guard.json: data-proxy first, R2-direct fallback. The proxy path
    whitelist is INF-owned and predates this artifact (D115: proxy 404s it), so CI
    reads it straight from the canonical R2 key when the proxy has not caught up."""
    g = proxy_json("sjd-liveness-guard.json")
    if g:
        return g
    try:
        import boto3
        s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
        raw = s3.get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key="data/sjd-liveness-guard.json")["Body"].read()
        return json.loads(raw)
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
    # Top-level verdict gate: some health-check failure classes append to issues[] + exit 1
    # WITHOUT setting any per-item verdict (critical-workflow failures, peer-lag edge cases) —
    # and the tracking-issue alert step itself can fail (observed 2026-08-17 15:07Z run
    # 32041303487: checks failed AND issue step failed → zero alert anywhere). Gating on the
    # aggregate verdict gives those classes an independent aspect-side signal (<=6h).
    verdict = (hc.get("verdict") or "").upper()
    if verdict == "FAIL":
        failing.append(f"verdict={verdict}")
    if not dests and not repos:
        return "YELLOW", "no destinations/repos in health-check", "proxy:out-health-check"
    _names = lambda lst: ", ".join(x for x in lst if x) or "none"
    return (green_if(not failing),
            f"{len(dests)} destinations + {len(repos)} consumer repos, {len(failing)} failing: {_names(failing)}"
            + (f" (incl. aggregate {verdict})" if verdict == "FAIL" else ""),
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
        checked = dl.get("total_checked", 0)
        # L3 scanner persists confirmed-dead tombstones across runs (rotation memory);
        # the gate measures PER-RUN findings, not accumulated memory.
        dead = dl.get("total_dead_fresh", dl.get("total_dead", 0))
        rate = dead / checked if checked else 1
        if rate >= 0.05: issues.append(("red", f"dead-link {rate*100:.1f}% ({dead}/{checked})"))
        elif rate >= 0.01: issues.append(("warn", f"dead-link {rate*100:.1f}% ({dead}/{checked})"))
    # 1b. Publish status — boards that failed to push. Catches the continue-on-error masking
    # (a failed board leaves the workflow GREEN; e.g. the IT GH_PAT 403 class). Absent artifact = cold start -> no issue.
    ps = proxy_json("publish-status.json")
    if ps:
        failed = ps.get("failed") or []
        if failed:
            issues.append(("red", f"boards failed to publish: {', '.join(failed)}"))
    # 2. Visa fill + undated rate from health check (catches enrichment failures across sampled repos)
    hc = proxy_json("out-health-check.json")
    if hc:
        for r in (hc.get("repos") or []):
            visa = r.get("readme_visa_pct")
            undated = r.get("readme_undated_pct")
            if visa is not None:
                repo_name = r.get("repo", "?")
                # Canada boards have NO visa column (US-H1B is meaningless for Canada-located jobs, D67) — skip the visa check for them
                if visa < 10 and not repo_name.startswith("Canada"): issues.append(("red", f"{repo_name} visa {visa}% (<10%)"))
                if undated is not None and undated > 80: issues.append(("red", f"{repo_name} undated {undated}% (>80%)"))
    # 3. Liveness agreement (family-closure checklist item 5, D115): the sjd build publishes
    # store-vs-store guard metrics (R2 data/sjd-liveness-guard.json). Staleness or a degraded
    # agreement means the two liveness stores (DB is_active vs served artifact) are drifting
    # again: the OUT-SJD-ARTIFACT-LIVENESS-DESYNC-1 class, previously invisible to checks.
    lg = guard_artifact()
    if not lg:
        issues.append(("warn", "sjd-liveness-guard.json unreachable (liveness agreement unmeasured)"))
    else:
        gen = lg.get("generated_at")
        try:
            dt = datetime.datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            age_h = (NOW - dt).total_seconds() / 3600
            if age_h > 48:
                issues.append(("warn", f"liveness guard artifact stale ({age_h:.0f}h old)"))
            matched = lg.get("full_time_db_matched") or 0
            dropped = lg.get("full_time_dropped_db_dead")
            # Bands on THIS metric's own basis (dropped/matched): healthy live reading
            # 21.8% (2026-10-08); the recorded pre-fix state recomputes to ~39.2% on this
            # basis from D111's drop record (the 78%/30.2% figures often cited were other
            # bases: dead-share-of-drops and dead%-of-artifact). Warn >30% = above any
            # healthy observation; red >45% = past the computed pre-fix state.
            try:
                if matched and dropped is not None:
                    frac = dropped / matched
                    if frac > 0.45: issues.append(("red", f"sjd full-time DB-dead drops {frac*100:.0f}% ({dropped}/{matched}, >45%)"))
                    elif frac > 0.30: issues.append(("warn", f"sjd full-time DB-dead drops {frac*100:.0f}% ({dropped}/{matched}, >30% band)"))
                    elif age_h <= 48: issues.append(("ok", f"liveness guard armed: fresh {age_h:.1f}h, DB-dead drops {frac*100:.0f}% ({dropped}/{matched})"))
            except TypeError:
                issues.append(("warn", "liveness guard metric fields unreadable (type drift)"))
        except (TypeError, ValueError):
            issues.append(("warn", "liveness guard artifact has unreadable generated_at"))
    reds = [m for s, m in issues if s == "red"]
    warns = [m for s, m in issues if s == "warn"]
    if reds: status = "RED"
    elif warns: status = "YELLOW"
    else: status = "GREEN"
    evidence = "; ".join(m for _, m in issues) if issues else "dead-link + visa + undated all healthy"
    return status, f"{evidence}", "proxy:dead-links+health-check"

def _registry_boards():
    # Consumer-board universe from the membership single source (OUT-BOARDLIST-SINGLE-SOURCE-1):
    # every non-infra registry entry carries render checks; class 'board' additionally gets
    # cover + cross-promo checks (curated/manual/community boards use their own heading
    # assets, D139 asset-vocabulary convention). A board swap edits the registry, not this.
    try:
        with open(REGISTRY) as f:
            reg = json.load(f)
        entries = reg.get("boards", reg.get("repos", []))
        return [(e["name"], e.get("class")) for e in entries if e.get("class") != "infra"]
    except Exception:
        return None

def _truth_one(repo, is_pipeline, pipeline_names):
    fails, warns = [], []
    d = gh_json(["api", f"repos/{ORG}/{repo}/contents/README.md"])
    if not isinstance(d, dict):
        return ["README unreadable"], []
    body = base64.b64decode(d.get("content", "")).decode("utf-8", "replace")
    t = gh_json(["api", f"repos/{ORG}/{repo}/git/trees/HEAD?recursive=1"])
    files = {e.get("path") for e in (t.get("tree", []) if isinstance(t, dict) else []) if e.get("type") == "blob"}
    srcs = re.findall(r'<img[^>]*?src="([^"]+)"', body) + re.findall(r'!\[[^\]]*\]\(([^)]+\.png)\)', body)
    broken = sorted({s for s in srcs if not s.startswith("http") and s not in files})
    if broken:
        fails.append(f"broken image refs: {', '.join(broken[:4])}")
    if is_pipeline and "images/covers/cover.png" not in files:
        fails.append("cover missing at images/covers/cover.png")
    bad = re.findall(r"\b(\d{2,})mo\b", body)
    if bad:
        fails.append(f"broken month-age tokens: {', '.join(sorted(set(bad))[:4])}")
    if is_pipeline:
        targets = set(re.findall(r"github\.com/%s/([A-Za-z0-9-]+)" % ORG, body))
        missing = [b for b in pipeline_names if b != repo and b not in targets]
        if missing:
            fails.append(f"cross-promo missing: {', '.join(missing[:4])}")
    desc = gh_json(["api", f"repos/{ORG}/{repo}"])
    if not ((desc or {}).get("description") or "").strip():
        warns.append("description empty (SEO/link-preview)")
    return fails, warns

def c_readme_truth():
    # Ported sweep leg (session out-board-truth.py ran weekly via out-ops sweep) -> CI hourly.
    # Catches the "looks broken to a visitor" class the day it happens, not at the next
    # session touch. Asset-manifest drift stays session-side (needs the job-board-assets
    # clone; that repo's own CI syncs assets). gh api failure -> README unreadable -> FAIL:
    # an unreadable README is exactly the visitor-visible subject of this aspect, and
    # gh_json's 3-attempt retry guards transient 5xx/rate-limit flake.
    universe = _registry_boards()
    if universe is None:
        return "YELLOW", "board-registry.json unreadable in processing submodule", "fs:board-registry"
    pipeline = [n for n, c in universe if c == "board"]
    fails, warns = [], []
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda x: _truth_one(x[0], x[1] == "board", pipeline), universe))
    for (repo, _cls), (f, w) in zip(universe, results):
        fails += [f"{repo}: {m}" for m in f]
        warns += [f"{repo}: {m}" for m in w]
    status = "RED" if fails else ("YELLOW" if warns else "GREEN")
    det = "; ".join((fails + warns)[:4])
    return status, f"{len(universe)} boards checked, {len(fails)} FAIL, {len(warns)} WARN" + (f": {det}" if det else ""), "gh-api:readme-truth"

def _fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "out-aspect-verifier/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def _stride(ids, n):
    ids = sorted(set(ids))
    return ids[::max(1, len(ids) // n)][:n] if ids else []

def _r2_json(key):
    try:
        import boto3
        s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
        return json.loads(s3.get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key)["Body"].read().decode("utf-8", "replace"))
    except Exception:
        return None

def _supabase_live(ids):
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        return None
    q = urllib.parse.quote(",".join('"%s"' % i for i in ids), safe="")
    req = urllib.request.Request(
        f"{url}/rest/v1/jobs?select=source_id,is_active&source=eq.GITHUB&source_id=in.({q})",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "User-Agent": "out-aspect-verifier/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rows = json.loads(r.read().decode("utf-8"))
    lookup = {}
    for row in rows if isinstance(rows, list) else []:
        sid = row.get("source_id")
        if sid:
            lookup[sid] = lookup.get(sid, False) or bool(row.get("is_active"))
    return lookup

def c_liveness_canary():
    # Ported rider (session out-liveness-canary.sh ran weekly) -> CI hourly, SAME rider rule:
    # any surface < 0.70 live escalates (the 09-14 zombie-class monitor). Thresholds are the
    # rider's own, not new policy (no operator sign-off needed); no YELLOW band above 0.70 is
    # added because D116 readings 0.69-0.82 are disposed accepted-class decay - a band would
    # put a permanent false-alarm color on the matrix. Session-side 12-run history/delta stays
    # session-side; CI publishes the per-run rate.
    canary = []
    for name, repo in (("ngj_readme", "New-Grad-Jobs-2027"), ("healthcare_readme", "New-Grad-Healthcare-Jobs-2027")):
        try:
            text = _fetch(f"https://raw.githubusercontent.com/{ORG}/{repo}/main/README.md")
            ids = re.findall(r"zapply\.jobs/l/d/([A-Za-z0-9._~-]+)", text)
            canary.append((name, _stride(ids, 20), len(set(ids)), None))
        except Exception as e:
            canary.append((name, [], 0, str(e)[:120]))
    try:
        art = _r2_json("data/sjd-jobs.json")
        if art is None:
            raise RuntimeError("R2 read failed")
        rows = art.get("jobs", art.get("data", [])) if isinstance(art, dict) else art
        ids = [r.get("id") for r in rows if isinstance(r, dict) and r.get("id")]
        canary.append(("sjd_artifact", _stride(ids, 60), len(set(ids)), None))
    except Exception as e:
        canary.append(("sjd_artifact", [], 0, str(e)[:120]))
    all_ids = sorted({i for _, s, _, _ in canary for i in s})
    lookup = _supabase_live(all_ids) if all_ids else {}
    if lookup is None:
        return "YELLOW", "SUPABASE_URL/SERVICE_ROLE_KEY unavailable to verifier step (canary unmeasured)", "supabase-rest"
    reds, parts = [], []
    for name, sampled, total, err in canary:
        if err:
            reds.append(f"{name} source-fail ({err})")
            continue
        known = [i for i in sampled if i in lookup]
        live = sum(1 for i in known if lookup[i])
        frac = (live / len(known)) if known else None
        if frac is None or len(known) < 5:
            reds.append(f"{name} insufficient sample (found {len(known)}/{len(sampled)})")
            continue
        parts.append(f"{name} {frac:.2f} ({live}/{len(known)}, missing {len(sampled) - len(known)})")
        if frac < 0.70:
            reds.append(f"{name} {frac:.2f} below rider 0.70")
    status = "RED" if reds else "GREEN"
    ev = "; ".join(parts) if parts else "no surface scored"
    if reds:
        ev = "; ".join(reds[:3]) + (" | " + ev if ev else "")
    return status, ev, "supabase-rest+gh-raw+R2"

# ── semantic_conformance rider (D146-cont; design: workspace state/generated/out_conformance_port_design_2026-11-06.md) ──
# CI owns gated legs 1+2 of out-semantic-conformance.js (legs 3/4 + judgment disposals stay
# session-side weekly). RED on WORSENING, not presence: leg-2 has been non-zero since 10-18
# (INF-LANDING-VISA-RAWCHAIN-1 owns the fix), so zero-tolerance hourly RED pages nobody
# usefully. Trigger: deduped leg-2 RATE, 2-strike ceiling, like-for-like denominator guard,
# era-aware zero-tolerance (resumes the session script's own gate law once the defect clears).
# Leg-1 (OUT-side artifact) instant-RED. Constants provisional; recalibrate on ~2 weeks of
# hourly rider_state. Sampling is LCG-seed-identical to the session script (20260830) for
# reproducibility; comparability across runs comes from the like-for-like denominator gate,
# not from identical draws (artifact arrays churn hourly).
_CONF = {"sample": 400, "companies": 150, "min_matched": 60, "ceiling": 0.25,
         "l4l": 0.20, "degrade_share": 0.20, "budget_s": 240,
         "landing": "https://zapply.jobs/api/jobs", "owner_row": "INF-LANDING-VISA-RAWCHAIN-1"}
_CONF_TO_SJD = {"job_sponsor": "sponsor", "company_sponsor": "possible", "none": "none"}
_SJD_MEMO = []
_RIDER_STATE = {}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

def _sjd_artifact():
    if not _SJD_MEMO:
        art = _r2_json("data/sjd-jobs.json")
        if art is not None:
            _SJD_MEMO.append(art)
    return _SJD_MEMO[0] if _SJD_MEMO else None

def _r2_json_or_jsonl(key):
    """enriched_jobs.json is newline-delimited JSONL (AGG pool dump), not one JSON
    document - json.loads whole-body fails with 'Extra data' (found live 11-06, T2).
    Try whole-document first, then per-line parse."""
    raw = _r2_json(key)
    if raw is not None:
        return raw
    try:
        import boto3
        s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
        body = s3.get_object(Bucket=os.environ["R2_BUCKET_NAME"], Key=key)["Body"].read().decode("utf-8", "replace")
        rows = []
        for line in body.split("\n"):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows or None
    except Exception:
        return None

def _conf_lcg():
    s = 20260830
    while True:
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        yield s / 4294967296.0

def _conf_pick(rng, arr, k):
    out = []
    n = len(arr)
    for i in range(n):
        if len(out) >= k:
            break
        if next(rng) < (k - len(out)) / (n - i):
            out.append(arr[i])
    return out

def _conf_resolve_ids(ids):
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        raise RuntimeError("supabase env unavailable")
    m = {}
    uuids = [i for i in ids if _UUID_RE.match(i)]
    for i in ids:
        if not _UUID_RE.match(i):
            m[i] = i
    for j in range(0, len(uuids), 50):
        chunk = uuids[j:j + 50]
        u = f"{url}/rest/v1/jobs?select=job_id,details->>sourceId&job_id=in.({','.join(chunk)})"
        req = urllib.request.Request(u, headers={"apikey": key, "Authorization": f"Bearer {key}",
                                                 "User-Agent": "out-aspect-verifier/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            for row in json.loads(r.read().decode("utf-8")):
                if row.get("sourceId"):
                    m[row["job_id"]] = row["sourceId"]
    return m

def _conf_leg1(sampled, sid, canon):
    compared = mismatches = parity = unresolved = lag = drift = 0
    samples = []
    mismatch_ids = []
    for row in sampled:
        c = canon.get(sid.get(row.get("id")))
        if not c:
            unresolved += 1
            continue
        compared += 1
        expected = _CONF_TO_SJD.get(c.get("visa_signal"), c.get("visa_signal"))
        if row.get("visaSignal") != expected:
            mismatches += 1
            mismatch_ids.append(row.get("id"))
            if len(samples) < 5:
                samples.append({"uuid": row.get("id"), "sourceId": sid.get(row.get("id")),
                                "canonical": c.get("visa_signal"), "sjd": row.get("visaSignal")})
        if bool(row.get("h1bSponsor")) != (row.get("visaSignal") == "sponsor"):
            parity += 1
    if mismatch_ids:
        url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
        queries = []
        uuid_ids = [i for i in mismatch_ids if _UUID_RE.match(i)]
        direct_ids = [i for i in mismatch_ids if not _UUID_RE.match(i)]
        for j in range(0, len(uuid_ids), 50):
            chunk = uuid_ids[j:j + 50]
            queries.append(f"{url}/rest/v1/jobs?select=job_id,details->enriched&job_id=in.({','.join(chunk)})")
        for j in range(0, len(direct_ids), 50):
            chunk = [urllib.parse.quote(i, safe="") for i in direct_ids[j:j + 50]]
            queries.append(f"{url}/rest/v1/jobs?select=job_id,source_id,details->enriched&source_id=in.({','.join(chunk)})")
        sjd_by_key = {r.get("id"): r for r in sampled}
        for u in queries:
            req = urllib.request.Request(u, headers={"apikey": key, "Authorization": f"Bearer {key}",
                                                     "User-Agent": "out-aspect-verifier/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                for row in json.loads(r.read().decode("utf-8")):
                    e = row.get("enriched") or {}
                    vs = e.get("visaSignal")
                    derived = (_CONF_TO_SJD.get(vs if vs is not None else "none", "none")
                               if vs is not None else
                               ("sponsor" if e.get("sponsorsVisa") is True else
                                "possible" if e.get("possibleSponsor") is True else "none"))
                    srow = sjd_by_key.get(row.get("source_id") or row.get("sourceId") or row.get("job_id"))
                    if srow is not None and srow.get("visaSignal") == derived:
                        lag += 1
                    else:
                        drift += 1
    return {"compared": compared, "drift": drift, "parity": parity, "lag": lag,
            "unresolved": unresolved, "samples": samples}

def _conf_leg2(sampled, sid, canon, rng):
    companies = []
    seen_co = set()
    for r in sampled:
        co = (r.get("company") or "").strip()
        if co and co not in seen_co:
            seen_co.add(co)
            companies.append(co)
    chosen = _conf_pick(rng, companies, min(_CONF["companies"], len(companies)))
    def key(co, ro):
        return f"{(co or '').lower().strip()}::{(ro or '').lower().strip()}"
    sjd_by_key = {key(r.get("company"), r.get("role")): r for r in sampled}
    landing_rows = []
    name_misses = 0
    fetch_failures = 0

    def fetch_one(co):
        # _fetch returns raw text (README/regex helper); the landing API speaks JSON.
        return co, json.loads(_fetch(f"{_CONF['landing']}/?company={urllib.parse.quote(co)}&per_page=50", timeout=20))

    deadline = time.monotonic() + _CONF["budget_s"]
    budget_blown = False
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(fetch_one, co) for co in chosen]
        for fut in futs:
            left = deadline - time.monotonic()
            if left <= 0:
                budget_blown = True
                fetch_failures += 1
                fut.cancel()
                continue
            try:
                co, page = fut.result(timeout=max(1, left))
                rows = page.get("jobs") or []
                if rows:
                    landing_rows.extend(rows)
                else:
                    name_misses += 1
            except Exception:
                fetch_failures += 1
    compared = mismatches = 0
    seen_keys = set()
    samples = []
    for lr in landing_rows:
        k = key(lr.get("company"), lr.get("role"))
        if k in seen_keys:
            continue  # dedupe: distinct (company,role) pairs only (design Decision 2)
        s = sjd_by_key.get(k)
        if s is None:
            continue
        c = canon.get(sid.get(s.get("id")))
        if not c:
            continue
        seen_keys.add(k)
        compared += 1
        expected = _CONF_TO_SJD.get(c.get("visa_signal"), c.get("visa_signal"))
        if lr.get("visaSignal") != expected:
            mismatches += 1
            if len(samples) < 5:
                samples.append({"company": lr.get("company"), "role": lr.get("role"),
                                "canonical": c.get("visa_signal"), "landing": lr.get("visaSignal")})
    return {"compared": compared, "mismatches": mismatches, "companies_attempted": len(chosen),
            "company_fetch_failures": fetch_failures, "company_name_misses": name_misses,
            "distinct": True, "samples": samples}

def _conformance_verdict(prev, cur):
    """Pure trigger (design Decision 2). Returns (status, trigger, like_for_like,
    era, zero_streak, reason). Era semantics: the Z clause is armed by the PREVIOUS
    state (>=2 consecutive zero runs); a run with mismatches then fires Z and
    publishes the reset (DIRTY, streak 0) for the next run."""
    leg1, leg2 = cur["leg1"], cur["leg2"]
    prev_streak = (prev or {}).get("zero_streak", 0)
    armed = prev_streak >= 2
    zero_streak = (prev_streak + 1) if (prev and leg2["mismatches"] == 0 and leg2["compared"] >= _CONF["min_matched"]) else 0
    era = "CLEAN" if zero_streak >= 2 else "DIRTY"
    prev_established = bool(prev and prev.get("baseline_established"))
    if prev is None or not prev_established:
        if prev is None:
            cur["baseline_established"] = False
            return "YELLOW", "baseline", False, era, zero_streak, "baseline establishing (first run)"
    else:
        cur["baseline_established"] = True
    if leg2["compared"] == 0 and leg1["compared"] == 0:
        return "RED", "U", False, era, zero_streak, "unmeasured: no comparisons resolved (sources/join dead)"
    if cur.get("unmeasured"):
        return "RED", "U", False, era, zero_streak, f"unmeasured: {cur['unmeasured']}"
    if leg1["drift"] > 0 or leg1["parity"] > 0:
        return "RED", "A", False, era, zero_streak, "leg1 drift/parity > 0 (zero-tolerance, OUT-side)"
    if armed and leg2["mismatches"] > 0:
        return "RED", "Z", False, era, zero_streak, "post-fix zero-tolerance: mismatch in CLEAN era"
    l4l = bool(prev and prev_established and prev.get("leg2") and
               abs(leg2["compared"] - prev["leg2"]["compared"]) <= _CONF["l4l"] * max(1, prev["leg2"]["compared"]))
    prev_rate_ge = bool(l4l and prev["leg2"]["compared"] and
                        prev["leg2"]["mismatches"] / prev["leg2"]["compared"] >= _CONF["ceiling"])
    cur_rate = leg2["mismatches"] / leg2["compared"] if leg2["compared"] else 0.0
    if l4l and prev_rate_ge and cur_rate >= _CONF["ceiling"]:
        return "RED", "T1", True, era, zero_streak, f"2-strike ceiling: rate {cur_rate:.1%} >= 25% twice"
    deg = (leg2["companies_attempted"] and
           leg2["company_fetch_failures"] / leg2["companies_attempted"] > _CONF["degrade_share"]) or cur.get("budget_blown")
    if 0 < leg2["compared"] < _CONF["min_matched"]:
        return "YELLOW", "thin", l4l, era, zero_streak, f"thin leg-2 ({leg2['compared']} < {_CONF['min_matched']})"
    if deg:
        return "YELLOW", "degraded", l4l, era, zero_streak, "degraded (fetch failures / budget)"
    return "GREEN", "none", l4l, era, zero_streak, f"leg2 rate {cur_rate:.1%} < 25% 2-strike ceiling"

def c_semantic_conformance():
    rng = _conf_lcg()
    try:
        enriched = _r2_json_or_jsonl("data/enriched_jobs.json")
        if enriched is None:
            raise RuntimeError("enriched R2 read failed")
        rows = enriched if isinstance(enriched, list) else enriched.get("jobs") or []
        canon = {r.get("id"): r for r in rows if isinstance(r, dict) and r.get("visa_signal")}
        art = _sjd_artifact()
        if art is None:
            raise RuntimeError("sjd artifact R2 read failed")
        sjd_rows = art if isinstance(art, list) else art.get("jobs") or art.get("data") or []
        sampled = _conf_pick(rng, sjd_rows, _CONF["sample"])
        sid = _conf_resolve_ids([r.get("id") for r in sampled if isinstance(r, dict) and r.get("id")])
        leg1 = _conf_leg1(sampled, sid, canon)
        leg2 = _conf_leg2(sampled, sid, canon, rng)
    except Exception as e:
        # F9 (adversary): source failures route through the unmeasured machinery (RED 1-strike
        # with reason + populated rider_state), never verify()'s crash-RED — the evidence line
        # and state must stay debuggable at the matrix.
        _RIDER_STATE["semantic_conformance"] = {
            "generated_at": NOW.isoformat(), "baseline_established": None,
            "era": "DIRTY", "unmeasured": str(e)[:160], "verdict": "RED", "trigger": "U",
        }
        return "RED", f"unmeasured: {str(e)[:160]} | owner: {_CONF['owner_row']}", \
            "py-port:legs1+2 of out-semantic-conformance.js (legs3+4 stay session-side)"
    cur = {"leg1": leg1, "leg2": leg2}
    prev_payload = proxy_json("out-aspect-status.json") or _r2_json("data/out-aspect-status.json")
    prev = (prev_payload.get("rider_state", {}).get("semantic_conformance")
            if isinstance(prev_payload, dict) else None)
    if prev is not None and prev.get("baseline_established") is None:
        prev = None  # malformed prev = baseline restart (fail-closed, design Decision 3)
    status, trigger, l4l, era, zero_streak, reason = _conformance_verdict(prev, cur)
    prev_l2 = (prev or {}).get("leg2") or {}
    prev_txt = (f"{prev_l2.get('mismatches')}/{prev_l2.get('compared')}"
                if prev_l2.get("compared") is not None else "n/a")
    rate = leg2["mismatches"] / leg2["compared"] if leg2["compared"] else 0.0
    _RIDER_STATE["semantic_conformance"] = {
        "generated_at": NOW.isoformat(),
        "baseline_established": cur.get("baseline_established", bool(prev)),
        "era": era, "zero_streak": zero_streak,
        "prev_run_rate_ge_ceiling": bool(l4l and prev_l2 and prev_l2.get("compared") and
                                         prev_l2["mismatches"] / prev_l2["compared"] >= _CONF["ceiling"]),
        "leg1": {k: leg1[k] for k in ("compared", "drift", "parity", "lag", "unresolved")},
        "leg2": {k: leg2[k] for k in ("compared", "mismatches", "companies_attempted",
                                      "company_fetch_failures", "company_name_misses", "distinct")},
        "verdict": status, "trigger": trigger, "like_for_like": l4l,
        "samples": (leg1["samples"] + leg2["samples"])[:5],
    }
    ev = (f"leg1 {leg1['drift']}/{leg1['compared']} drift, {leg1['parity']} parity ({leg1['lag']} lag) | "
          f"leg2 {leg2['mismatches']}/{leg2['compared']} distinct ({rate:.1%}) "
          f"prev {prev_txt} | trigger {trigger} | "
          f"owner: {_CONF['owner_row']} | {reason}")
    return status, ev, "py-port:legs1+2 of out-semantic-conformance.js (legs3+4 stay session-side)"

CHECKS = {
    "verification": c_verification, "monitoring": c_monitoring, "performance": c_performance,
    "infrastructure": c_infrastructure, "configuration": c_configuration,
    "discoverability": c_discoverability, "security": c_security, "documentation": c_documentation,
    "change_mgmt": c_change_mgmt, "data_quality": c_data_quality,
    "readme_truth": c_readme_truth, "liveness_canary": c_liveness_canary,
    "semantic_conformance": c_semantic_conformance,
}

def verify():
    aspects = {}
    for name, fn in CHECKS.items():
        try:
            status, evidence, source = fn()
        except Exception as e:
            status, evidence, source = "RED", f"check error: {e}", "verifier-error"
        aspects[name] = {"status": status, "evidence": evidence, "source": source}
    result = {"module": "OUT", "generated_at": NOW.isoformat(), "aspects": aspects}
    if _RIDER_STATE:
        result["rider_state"] = _RIDER_STATE
    return result

if __name__ == "__main__":
    result = verify()
    print(json.dumps(result, indent=2))
    counts = {}
    for a in result["aspects"].values():
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    print(f"\nSummary: {counts}", file=sys.stderr)
