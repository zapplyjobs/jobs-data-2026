#!/usr/bin/env python3
# CANONICAL: this jobs-data-2026 copy publishes the official artifact (data/out-aspect-status.json on R2; Supabase-storage mirror pipeline-data/). The old GenAI_Work workspace mirror at projects/zjp/scripts/ was TOMBSTONED 11-05 (16-line pointer, OUT-ASPECTVERIFIER-MIRROR-DRIFT-1) - never resurrect it; session-side instruments are deep-pass only (sole-measurer law, D148 leg-4). Session-side code-fact citations use the canonical clone /home/ahd/zjp-repos/jobs-data-2026 (clone discipline, D146). Header trued 11-10 D158: the pre-11-10 warning named the mirror as live, which it no longer is.
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
  asset_drift (2026-11-10, PHASE2 leg-2) -> manifest-vs-board sha equality, presence-RED
  discord_conformance (2026-11-10, hole#2 closer) -> posting latency/miss/repost/routing/
    verifier-liveness classes; MONITORING ONLY, never mutates posting (operator-gated)
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

_TREE_MEMO = {}

def _repo_tree(repo):
    # Blob-sha map {path: sha} for one repo HEAD tree, memoized per run. Shared by
    # readme_truth (path existence) and asset_drift (sha equality) so an hourly run makes
    # ONE tree call per repo (~20 total) instead of one per check (430-call hazard if
    # asset_drift ever fetched per-target contents).
    if repo not in _TREE_MEMO:
        t = gh_json(["api", f"repos/{ORG}/{repo}/git/trees/HEAD?recursive=1"])
        _TREE_MEMO[repo] = ({e.get("path"): e.get("sha")
                             for e in (t.get("tree", []) if isinstance(t, dict) else [])
                             if e.get("type") == "blob"} if isinstance(t, dict) else None)
    return _TREE_MEMO[repo]

def _truth_one(repo, is_pipeline, pipeline_names):
    fails, warns = [], []
    d = gh_json(["api", f"repos/{ORG}/{repo}/contents/README.md"])
    if not isinstance(d, dict):
        return ["README unreadable"], []
    body = base64.b64decode(d.get("content", "")).decode("utf-8", "replace")
    files = _repo_tree(repo)
    files = set(files) if isinstance(files, dict) else set()
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

def c_asset_drift():
    # PHASE2 leg-2 (D148 port; design verdict Phase2Judge 2026-11-10): the unported check #6 of
    # session out-board-truth.py - sha-equality of every job-board-assets MANIFEST.json-declared
    # target against the asset canon. Presence-based instant-RED (NOT the leg-1 worsening class):
    # drift is binary and each instance is visitor-visible breakage (broken covers/listings
    # banners on live boards). Canon shas come from the manifest's own git_blob_sha field
    # (content-addressed, repo-independent) - one manifest call + one memoized tree call per
    # target repo; no local hashing, no clone (the session script hashed clone bytes; deriving
    # from remote trees removes the D146 stale-clone citation hazard class). The manifest lives
    # in the PRIVATE zapplyjobs/job-board-assets repo; the workflow's GH_TOKEN (secrets.GH_PAT)
    # reads it - sync.yml reads the same repo with the same secret. Failure tiers: manifest or
    # any target tree unreadable after gh_json's 3-attempt retry -> RED unmeasured (fail-closed,
    # transient gh flake must never read as drift); a canon push completes before sync.yml lands
    # -> possible 1-hour transient RED that self-heals next run (writer: job-board-assets
    # sync.yml, on push).
    d = gh_json(["api", f"repos/{ORG}/job-board-assets/contents/MANIFEST.json"])
    if not isinstance(d, dict) or not d.get("content"):
        return "RED", f"unmeasured: MANIFEST.json unreadable (gh api after retries) - drift check degraded", "gh-api:job-board-assets"
    try:
        manifest = json.loads(base64.b64decode(d["content"]).decode("utf-8", "replace"))
    except Exception as e:
        return "RED", f"unmeasured: MANIFEST.json unparseable ({str(e)[:80]})", "gh-api:job-board-assets"
    assets = manifest.get("assets") if isinstance(manifest, dict) else None
    if not isinstance(assets, list) or not assets:
        return "RED", "unmeasured: manifest carries no assets array", "gh-api:job-board-assets"
    drifts, unreadable, checked = [], [], 0
    for a in assets:
        canon_sha = a.get("git_blob_sha")
        for t in a.get("targets", []) if isinstance(a, dict) else []:
            repo = (t.get("repo") or "").removeprefix(f"{ORG}/")
            path = t.get("path")
            if not repo or not path or not canon_sha:
                continue
            tree = _repo_tree(repo)
            if not isinstance(tree, dict):
                unreadable.append(f"{repo} (tree)")
                continue
            checked += 1
            if tree.get(path) != canon_sha:
                drifts.append(f"{repo}:{path}")
    if unreadable:
        return "RED", f"unmeasured: target trees unreadable after retries: {'; '.join(unreadable[:3])}", "gh-api:trees"
    if drifts:
        return "RED", f"{len(drifts)} drifted target(s) of {checked}: {'; '.join(drifts[:4])}", "gh-api:manifest+trees"
    return "GREEN", f"{checked} manifest targets sha-match canon ({len(assets)} assets)", "gh-api:manifest+trees"

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

def _canary_thresholds():
    # Registry-driven canary thresholds (PHASE2 leg-3, D148): one versioned JSON both runners
    # read verbatim - CI here, session out-liveness-canary.sh from its canonical clone path.
    # Operator word -> JSON edit -> both runners re-point; no code change (D147
    # AlarmDoctrineResearcher choice 5). Defaults below mirror the pre-file settled law.
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "out-canary-thresholds.json")) as f:
            t = json.load(f)
        if isinstance(t, dict) and t.get("rider_threshold"):
            return t
    except Exception:
        pass
    return {"rider_threshold": 0.70, "hc_floor": 0.7368, "day_rule": "last",
            "escalation_class": "yellow", "history_depth": 72}

def _consec_days(day_list, ref):
    """Longest run of consecutive calendar days in day_list ending at or before ref.
    String-domain throughout (OUT-LIVENESS-CONSECDAYS-TYPEBUG-1): day_list carries
    h['ts'][:10] day STRINGS while ref is now_d = NOW.date() (a datetime.date); the old
    mixed-type filter `d <= ref` raised TypeError uncaught -> whole liveness_canary check
    false-RED once history carried healthcare readings (live 11-21, runs 44847685489+)."""
    ref_s = ref.isoformat() if isinstance(ref, datetime.date) else str(ref)[:10]
    ds = {str(d)[:10] for d in day_list}
    n, cur = 0, ref_s
    while cur in ds:
        n += 1
        cur = (datetime.date.fromisoformat(cur) - datetime.timedelta(days=1)).isoformat()
    return n

def score_liveness(canary, lookup, th):
    """Pure scoring core (D163 re-anchor bundle, row OUT-CANARY-REANCHOR-BUILD-1; fixtures drive
    it workspace-side - this repo carries no test harness). Returns (reds, parts, scores, detail)
    where detail[name] = {live, known, missing, frac}. Alarm law (operator word 2026-11-14):
    RED iff a surface reads below alarm_floor (the boundary moved BELOW the accepted baseline
    band) OR a surface misses >= missing_fire sampled ids (the already-collected
    missing_from_jobs removal signature: quiet baseline 0-1, fire >=4). In-band readings stay
    off the alarm channel and carry an in-band note. alarm_floor is PROVISIONAL until the
    11-20 recalibration ratifies constants by JSON edit (data, never code)."""
    alarm_floor = th.get("alarm_floor", th.get("rider_threshold", 0.70))
    missing_fire = th.get("missing_fire", 4)
    reds, parts, scores, detail = [], [], {}, {}
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
        missing = len(sampled) - len(known)
        detail[name] = {"live": live, "known": len(known), "missing": missing, "frac": frac}
        band = th.get("accepted_band") or [0.69, 0.82]
        in_band = bool(band[0] <= frac <= band[1])
        parts.append(f"{name} {frac:.2f} ({live}/{len(known)}, missing {missing}"
                     + (", in accepted band" if in_band else "") + ")")
        scores[name] = frac
        if frac < alarm_floor:
            reds.append(f"{name} {frac:.2f} below alarm floor {alarm_floor}")
        if missing >= missing_fire:
            reds.append(f"{name} removal-signature (missing {missing} >= fire {missing_fire})")
    return reds, parts, scores, detail


def zombie_candidates(sampled_ids, lookup, dead_repos):
    """One-line zombie side-car (operator word 2026-11-14; the founding 09-14 class): a job the
    boards render, the DB keeps active, and the nightly scan reports dead (dead-links.json
    'repo' field IS the source id - D163 fixture-verified). Candidate evidence only: session
    adjudication (class yellow, never pages), same law as the hc-floor watch."""
    return sorted(i for i in sampled_ids if lookup.get(i) and i in dead_repos)


def c_liveness_canary():
    # Ported rider (session out-liveness-canary.sh ran weekly) -> CI hourly. Re-anchored per the
    # operator word 2026-11-14 (D163): the alarm boundary lives BELOW the accepted baseline band
    # (out-canary-thresholds.json alarm_floor, provisional), the missing_from_jobs removal
    # signature fires the alarm, and the zombie class gets its own candidate side-car below.
    # Accepted-band readings stay off the alarm channel (D116 disposals). No YELLOW band is
    # added (D146: permanent false-alarm color). Session-side 12-run history/delta stays
    # session-side; CI publishes the per-run rate.
    canary = []
    TH = _canary_thresholds()
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
    reds, parts, scores, detail = score_liveness(canary, lookup, TH)
    if "alarm_floor" not in TH:
        # D163: silent-default fallback class - the crypt-filtered JSON is unreadable in this
        # environment (CI without transcrypt unlock). Observable in evidence, never silent.
        parts.append("thresholds: built-in defaults (canary JSON unreadable here)")
    # Zombie side-car inputs: dead-links.json is the nightly scan's real store (checked
    # universe ~8.5k); its 'repo' field is the source id (D163 live-verified: exact joins
    # against the sjd id universe). The alive-hold store (scan-verdicts.json) is URL-keyed
    # and records alive verdicts only - source ids cannot join against it, so absence there
    # proves nothing and it is NOT the side-car input.
    dead = _r2_json("data/dead-links.json")
    dead_repos = ({e.get("repo") for e in (dead or {}).get("dead_links", []) if isinstance(e, dict)}
                  if isinstance(dead, dict) else set())
    zombies = zombie_candidates(all_ids, lookup, dead_repos) if dead_repos else []
    status = "RED" if reds else "GREEN"
    ev = "; ".join(parts) if parts else "no surface scored"
    if reds:
        ev = "; ".join(reds[:3]) + (" | " + ev if ev else "")

    # PHASE2 leg-3 (D148 port; design verdict Phase2Judge): rolling intraday history + the
    # mechanized hc-floor watch. AMENDED 11-14 (D163, operator word on the semantics verdict):
    # the re-anchor bundle is LANDED LAW - alarm_floor (provisional, 11-20 ratifies via JSON
    # edit) + missing_fire + the zombie side-car below. The watch consequence stays session
    # adjudication ("-> investigate", D146 pre-commitment), so it NEVER colors this aspect
    # (no YELLOW band: D146 disposed that as a permanent false-alarm color) - it lands in
    # rider_state + evidence for the next session touch and the sweep's rider legs.
    now_d = NOW.date()
    entry = {"ts": NOW.isoformat(), **{k: (round(v, 4) if isinstance(v, float) else None)
                                       for k, v in scores.items()}}
    prev_rider = (proxy_json("out-aspect-status.json") or _r2_json("data/out-aspect-status.json")
                  or {}).get("rider_state", {}).get("liveness_canary", {})
    prev_hist = prev_rider.get("history") or []
    hist = (prev_hist + [entry])[-TH["history_depth"]:]
    hc_by_day = {}
    for h in hist:
        if h.get("healthcare_readme") is None:
            continue
        hc_by_day[h["ts"][:10]] = h["healthcare_readme"]  # day_rule=last reading of the day wins
    watch_days = [d for d, v in sorted(hc_by_day.items()) if v < TH["hc_floor"]]
    consec = _consec_days(watch_days, now_d) >= 2
    _RIDER_STATE["liveness_canary"] = {
        "generated_at": NOW.isoformat(),
        "scores": {k: round(v, 4) for k, v in scores.items()},
        "alarm_floor": TH.get("alarm_floor"),
        "zombie_sidecar": {"candidates": zombies[:10], "checked": len(all_ids),
                           "dead_store": bool(dead_repos), "class": "yellow"},
        "hc_floor_watch": {"fired": bool(consec),
                           "below_days": watch_days[-5:],
                           "floor": TH["hc_floor"], "class": TH["escalation_class"]},
        "history": hist,
        "history_baseline_reset": prev_hist == [],
    }
    if consec:
        ev += (f" | hc-floor watch FIRED ({_consec_days(watch_days, now_d)} consecutive days "
               f"< {TH['hc_floor']}; class {TH['escalation_class']}: session adjudication, not a page)")
    if zombies:
        ev += (f" | zombie side-car candidates {len(zombies)} (DB-active + rendered + scan-dead; "
               f"class yellow: session adjudication, not a page)")
    return status, ev, "supabase-rest+gh-raw+R2"

# ── semantic_conformance rider (D146-cont; design: workspace state/generated/out_conformance_port_design_2026-11-06.md) ──
# CI owns gated legs 1+2 of out-semantic-conformance.js (legs 3/4 + judgment disposals stay
# session-side weekly). RED on WORSENING, not presence: leg-2 has been non-zero since 10-18
# (INF-EXTERNAL-WAITS-1 L16 carries the fix wait), so zero-tolerance hourly RED pages nobody
# usefully. Trigger: deduped leg-2 RATE, 2-strike ceiling, like-for-like denominator guard,
# era-aware zero-tolerance (resumes the session script's own gate law once the defect clears).
# Leg-1 (OUT-side artifact) instant-RED. Constants provisional; recalibrate on ~2 weeks of
# hourly rider_state. Sampling is LCG-seed-identical to the session script (20260830) for
# reproducibility; comparability across runs comes from the like-for-like denominator gate,
# not from identical draws (artifact arrays churn hourly).
_CONF = {"sample": 400, "companies": 150, "min_matched": 60, "ceiling": 0.25,
         "l4l": 0.20, "degrade_share": 0.20, "budget_s": 240,
         "landing": "https://zapply.jobs/api/jobs",
         # Owner is a durable surface (module, lane, carrier line), never a task row: task rows close.
         "owner": {"module": "OUT", "lane": "visa signal conformance (leg-2 landing labels)",
                   "carrier": "INF-EXTERNAL-WAITS-1 L16", "fix_owner": "dev team (RD-20261103-03)"}}
# Discord conformance thresholds (D148; contract SLA "within ~15 min of pool update" now
# measured - the soft contract number is codified here, flagged for the operator's quarterly
# threshold-audit visibility and the 11-16 decision pass; measurement only, no behavior change).
_DISCORD = {"late_min": 15, "very_late_min": 45, "miss_min": 90,
            "artifact_stale_warn_min": 60, "artifact_stale_red_min": 180,
            "runs_post": 110, "runs_fetch": 130, "receipt_stride_h": 6}
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
    # OUT-SEMCONF-LEG1-PROPWINDOW-1: leg-1 zero-tolerance keeps instant-RED for real drift
    # (>1 row, any parity - parity is artifact-internal, a propagation window cannot cause it),
    # but an EXACTLY-1-row drift first runs the caller's propagation guard (grace re-fetch +
    # strike). Caller writes cur["leg1_strike"]: 0 = healed-or-clean, n = n-th consecutive
    # unhealed 1-row drift run. RED only on strike >= 2 (second consecutive hourly run).
    # The grace branch sits BELOW trigger Z so a CLEAN-era leg-2 mismatch still REDs instantly
    # (original A-before-Z priority preserved for every instant-RED class).
    if leg1["drift"] > 1 or leg1["parity"] > 0:
        return "RED", "A", False, era, zero_streak, "leg1 drift/parity > 0 (zero-tolerance, OUT-side)"
    if armed and leg2["mismatches"] > 0:
        return "RED", "Z", False, era, zero_streak, "post-fix zero-tolerance: mismatch in CLEAN era"
    if leg1["drift"] == 1:
        strike = int(cur.get("leg1_strike") or 0)
        if strike >= 2:
            return "RED", "A", False, era, zero_streak, \
                f"leg1 1-row drift persisted strike {strike}/2 (propagation window ruled out)"
        return "YELLOW", "A1", False, era, zero_streak, \
            f"leg1 1-row drift: propagation guard, grace re-fetch not healed, strike {strike}/2 (next run adjudicates)"
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
        return "RED", f"unmeasured: {str(e)[:160]} | " \
                      f"owner: {'; '.join(f'{k}={v}' for k, v in _CONF['owner'].items())}", \
            "py-port:legs1+2 of out-semantic-conformance.js (legs3+4 stay session-side)"
    prev_payload = proxy_json("out-aspect-status.json") or _r2_json("data/out-aspect-status.json")
    prev = (prev_payload.get("rider_state", {}).get("semantic_conformance")
            if isinstance(prev_payload, dict) else None)
    if prev is not None and prev.get("baseline_established") is None:
        prev = None  # malformed prev = baseline restart (fail-closed, design Decision 3)
    # OUT-SEMCONF-LEG1-PROPWINDOW-1 propagation guard: an hourly enrichment sweep can land
    # after the sjd-jobs.json artifact build, leaving EXACTLY-1-row leg1 drift as a transient
    # propagation window (live 11-20 23:04Z RED, healed by the next artifact build 00:04Z).
    # On drift==1/parity==0: one in-run grace re-fetch of sjd-jobs.json (memo bypassed) and a
    # re-test of the SAME sampled rows; healed -> no RED, strike reset. Still drifted -> strike
    # = consecutive unhealed runs (rider_state leg1_strike); verdict REDs at strike >= 2.
    # Re-check failure == cannot prove healing (original leg1 stands, strike accrues).
    strike = 0
    if leg1["drift"] == 1 and leg1["parity"] == 0:
        try:
            _SJD_MEMO.clear()
            art2 = _sjd_artifact()
            if art2 is None:
                raise RuntimeError("grace re-fetch returned nothing")
            rows2 = art2 if isinstance(art2, list) else art2.get("jobs") or art2.get("data") or []
            by_id = {r.get("id"): r for r in rows2 if isinstance(r, dict)}
            sampled2 = [by_id.get(r.get("id")) or r for r in sampled]
            leg1 = _conf_leg1(sampled2, sid, canon)
        except Exception:
            pass
        if leg1["drift"] == 0 and leg1["parity"] == 0:
            strike = 0  # healed in-run: no RED, no strike
        else:
            strike = int((prev or {}).get("leg1_strike") or 0) + 1
    cur = {"leg1": leg1, "leg2": leg2, "leg1_strike": strike}
    status, trigger, l4l, era, zero_streak, reason = _conformance_verdict(prev, cur)
    prev_l2 = (prev or {}).get("leg2") or {}
    prev_txt = (f"{prev_l2.get('mismatches')}/{prev_l2.get('compared')}"
                if prev_l2.get("compared") is not None else "n/a")
    rate = leg2["mismatches"] / leg2["compared"] if leg2["compared"] else 0.0
    _RIDER_STATE["semantic_conformance"] = {
        "generated_at": NOW.isoformat(),
        "baseline_established": cur.get("baseline_established", bool(prev)),
        "era": era, "zero_streak": zero_streak, "leg1_strike": strike,
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
          f"owner: {'; '.join(f'{k}={v}' for k, v in _CONF['owner'].items())} | {reason}")
    return status, ev, "py-port:legs1+2 of out-semantic-conformance.js (legs3+4 stay session-side)"

def _runs_full(repo, workflow, limit, fields="status,conclusion,createdAt,startedAt,updatedAt,event"):
    out = gh_json(["run", "list", "-R", repo, "-w", workflow, "-L", str(limit),
                   "--json", fields])
    return [r for r in (out or []) if r.get("status") == "completed"]

def _supabase_metric(key):
    # pipeline_metrics single row (key, data) - the DASH /api/data reader's exact shape.
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key_env = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key_env:
        return None
    try:
        q = urllib.parse.quote(f"key=eq.{key}&select=key,data", safe="=&")
        req = urllib.request.Request(
            f"{url}/rest/v1/pipeline_metrics?{q}",
            headers={"apikey": key_env, "Authorization": f"Bearer {key_env}",
                     "User-Agent": "out-aspect-verifier/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read().decode("utf-8"))
        return (rows[0] or {}).get("data") if rows else None
    except Exception:
        return None

def _iso_min(a, b):
    """Minutes from ISO datetime a to b (b - a, negative if a later)."""
    return (datetime.datetime.fromisoformat(b.replace("Z", "+00:00"))
            - datetime.datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() / 60.0

def c_discord_conformance():
    # PHASE2 / hole#2 closer (D148; design DiscordDesigner): Discord posting conformance as a
    # measured aspect. MONITORING ONLY - it must never mutate posting cadence/copy/routing
    # (user-visible, operator-gated). Classes: LATE/VERY-LATE (pool->post latency, gh run
    # pairing), MISS (fetch moving, Discord silent >=90m), REPOST + CONTENT-CRITICAL (R2
    # discord-verification.json critical counts - the consumer that never existed), VERIFIER-
    # DEAD (post-linked verification artifact staleness; the DAILY cron artifact is IGNORED by
    # design - it is almost always >60m old and must not false-RED), ROUTE-ERR (receipt
    # channelId vs expected-channels, 6h stride; residual: pre-deletion wrong-channel moves are
    # invisible at receipt level - filling verify-discord's verifyRouting stub is a dev-team
    # relay, not an OUT build). Transient late events are budget noise (GH runner jitter);
    # sustained silence pages. Self-heal: a late post downgrades MISS to late evidence.
    posts = _runs_full(f"{ORG}/jobs-data-2026", "post-to-discord.yml", _DISCORD["runs_post"])
    if not posts:
        return "RED", "unmeasured: no completed post-to-discord runs visible (gh api)", "gh-api:runs"
    now_iso = NOW.isoformat()
    window_start = (NOW - datetime.timedelta(hours=24)).isoformat()
    all_posts = sorted(posts, key=lambda r: r["createdAt"])
    win_posts = [r for r in all_posts if r["createdAt"] >= window_start]
    fetches = _runs_full(f"{ORG}/jobs-aggregator-private", "fetch-jobs.yml",
                         _DISCORD["runs_fetch"], fields="status,conclusion,createdAt,updatedAt")
    fetch_read_ok = bool(fetches)
    # The honest measured window is the INTERSECTION of what both run lists actually cover
    # (at a ~15-min post cadence a fixed -L tail may not reach a full 24h; claiming 24h on a
    # 6h sample manufactured false MISS windows on the first local run - D148 correction).
    span_start = max(window_start,
                     all_posts[0]["createdAt"] if all_posts else window_start,
                     min((f.get("createdAt", window_start) for f in fetches), default=window_start))
    win_posts = [r for r in all_posts if r["createdAt"] >= span_start]
    win_fetch = [f for f in fetches if f.get("updatedAt") and f["updatedAt"] >= span_start]
    span_h = round(_iso_min(span_start, now_iso) / 60, 1)
    # LATE / VERY-LATE + pairing gaps: for each window post run, latency = post start minus
    # the latest fetch COMPLETION at-or-before it (fetch wave-links enrich + discord).
    lates, very_lates, latencies = 0, 0, []
    f_done = sorted((f["updatedAt"] for f in win_fetch if f.get("conclusion") == "success"))
    for p in sorted(win_posts, key=lambda r: r["createdAt"]):
        prior = [t for t in f_done if t <= p["createdAt"]]
        if not prior:
            continue
        gap = _iso_min(prior[-1], p["createdAt"])
        latencies.append(gap)
        if gap > _DISCORD["late_min"]:
            lates += 1
        if gap > _DISCORD["very_late_min"]:
            very_lates += 1
    # MISS: longest fetch-completion -> next-post-start gap in window (unrecovered silence).
    miss_windows, max_gap = 0, 0.0
    if f_done:
        p_starts = sorted(p["createdAt"] for p in win_posts)
        for t in f_done:
            nxt = next((s for s in p_starts if s >= t), None)
            g = _iso_min(t, nxt) if nxt else _iso_min(t, now_iso)
            max_gap = max(max_gap, g)
            if g > _DISCORD["miss_min"]:
                miss_windows += 1
    # REPOST + CONTENT-CRITICAL + artifact freshness (post-linked only).
    art = _r2_json("data/discord-verification.json")
    reposts = content_crit = 0
    art_note = "artifact unreadable"
    if isinstance(art, dict):
        cc = art.get("criticalIssuesCount") or {}
        reposts = cc.get("recentSameIdReposts") or 0
        content_crit = sum(cc.get(k) or 0 for k in
                           ("locationErrors", "injectionErrors", "routingErrors")) \
                      + (1 if art.get("hasCriticalIssues") and not (reposts or cc.get("locationErrors")
                         or cc.get("injectionErrors") or cc.get("routingErrors")) else 0)
        art_note = f"artifact {art.get('timestamp', '?')}"
    verify_runs = _runs_full(f"{ORG}/jobs-data-2026", "verify-discord.yml", 10)
    linked = [r for r in verify_runs if r.get("event") == "workflow_run"]
    vdead = ""
    if linked and linked[0].get("conclusion") != "success":
        vdead = "post-linked verify run FAILED"
    latest_post_done = max((p["updatedAt"] for p in win_posts), default=None)
    if latest_post_done and isinstance(art, dict):
        age = _iso_min(latest_post_done, art.get("timestamp", now_iso))
        if age > 0:  # artifact predates the newest post run = its verification never landed
            if age > _DISCORD["artifact_stale_red_min"]:
                vdead = vdead or f"verification artifact stale {age:.0f}m past newest post run"
            elif age > _DISCORD["artifact_stale_warn_min"]:
                vdead = vdead or f"verification artifact pending {age:.0f}m past newest post run"
    # ROUTE-ERR receipt audit at stride (posted_jobs.json is MB-scale; 6h keeps egress small).
    route_err, audited = 0, 0
    prev_rider = (proxy_json("out-aspect-status.json") or _r2_json("data/out-aspect-status.json")
                  or {}).get("rider_state", {}).get("discord_conformance", {})
    last_audit = prev_rider.get("last_receipt_audit_at")
    due = (not last_audit) or _iso_min(last_audit, now_iso) >= _DISCORD["receipt_stride_h"] * 60
    if due:
        exp = _supabase_metric("expected-channels")
        if isinstance(exp, dict):
            known_ids = {c.get("id") for c in exp.get("channels", []) if isinstance(c, dict)}
            pj = _r2_json("data/posted_jobs.json")
            for job in ((pj or {}).get("jobs") or [])[:500]:
                for cid, meta in ((job.get("discordPosts") or {}).items()):
                    audited += 1
                    if cid not in known_ids:
                        route_err += 1
        last_audit = now_iso
    reds, ylows = [], []
    if miss_windows:
        reds.append(f"MISS: {miss_windows} fetch->post gap(s) > {_DISCORD['miss_min']}m (max {max_gap:.0f}m)")
    if reposts:
        reds.append(f"REPOST: {reposts} sameIdReposts")
    if content_crit:
        reds.append(f"CONTENT-CRITICAL: {content_crit}")
    if "FAILED" in vdead or "stale" in vdead:
        reds.append(vdead)
    elif vdead:
        ylows.append(vdead)
    if very_lates:
        ylows.append(f"{very_lates} very-late post start(s) > {_DISCORD['very_late_min']}m after pool")
    elif lates > 5:
        ylows.append(f"{lates} late post starts > {_DISCORD['late_min']}m (budget 5/24h)")
    if route_err:
        reds.append(f"ROUTE-ERR: {route_err} receipt(s) to non-expected channel ids")
    if not fetch_read_ok:
        ylows.append("fetch runs unreadable (private repo scope) - latency/miss classes unmeasured this run")
    p50 = sorted(latencies)[len(latencies) // 2] if latencies else None
    status = "RED" if reds else ("YELLOW" if ylows else "GREEN")
    ev = (f"{span_h}h window: {len(win_posts)} posts, {miss_windows} miss") \
        + (f", p50 lat {p50:.0f}m" if p50 is not None else "") \
        + f", {lates} late/{very_lates} very-late, {reposts} repost, {route_err} route-err" \
        + f"; {art_note}" \
        + (f"; receipts audited {audited} (stride {last_audit[:16] if last_audit else 'n/a'})") \
        + f"; thresholds {_DISCORD['late_min']}/{_DISCORD['very_late_min']}/{_DISCORD['miss_min']}m" \
        + (" | " + "; ".join((reds + ylows)[:3]) if (reds or ylows) else "")
    _RIDER_STATE["discord_conformance"] = {
        "generated_at": now_iso,
        "window_hours": span_h,
        "events": len(win_posts), "late": lates, "very_late": very_lates,
        "miss_windows": miss_windows, "max_gap_min": round(max_gap, 1),
        "repost": reposts, "route_err": route_err, "content_critical": content_crit,
        "p50_latency_min": round(p50, 1) if p50 is not None else None,
        "artifact": art_note, "fetch_runs_read": fetch_read_ok,
        "last_receipt_audit_at": last_audit,
        "thresholds": dict(_DISCORD),
    }
    return status, ev, "gh-api:runs+R2:discord-verification+supabase:expected-channels"

CHECKS = {
    "verification": c_verification, "monitoring": c_monitoring, "performance": c_performance,
    "infrastructure": c_infrastructure, "configuration": c_configuration,
    "discoverability": c_discoverability, "security": c_security, "documentation": c_documentation,
    "change_mgmt": c_change_mgmt, "data_quality": c_data_quality,
    "readme_truth": c_readme_truth, "liveness_canary": c_liveness_canary,
    "semantic_conformance": c_semantic_conformance,
    "asset_drift": c_asset_drift, "discord_conformance": c_discord_conformance,
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
