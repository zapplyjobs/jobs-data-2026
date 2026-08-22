#!/usr/bin/env python3
"""
OUT dead-link scanner (extracted from out-dead-link-check.yml heredoc 2026-08-16,
OUT-DEADLINK-CODECLEANUP-1 — same logic, lintable/testable as a file).

Weekly scan across consumer repos (2027 + 2026-alias names) + ATS availability
passes (Workday tenant-listing membership via wd-fetch-proxy; Ashby posting API).
Publishes dead-links.json to Supabase Storage + R2 (data/ prefix, canonical).
Exit 1 only when dead links found (triggers the tracking issue step in the workflow).
Output schema: {generated_at, total_checked, total_dead, dead_links:[{repo,url,http_code,closed_via?,note?}],
               ats_watch:[...], ats_stats:{judged,alive,dead,unknown,(pass?)} , total_transient, transient_links}
"""
import json, urllib.request, urllib.error, urllib.parse, re, os, sys, datetime, time
from concurrent.futures import ThreadPoolExecutor, as_completed


now = datetime.datetime.now(datetime.timezone.utc)
repos = [
    # 2027 boards
    "New-Grad-Jobs-2027", "Internships-2027",
    "New-Grad-Software-Engineering-Jobs-2027", "New-Grad-Data-Science-Jobs-2027",
    "New-Grad-Hardware-Engineering-Jobs-2027", "New-Grad-Healthcare-Jobs-2027",
    "New-Grad-IT-Jobs-2027",
    "Canada-Jobs-2027", "Canada-Internships-2027",
    # 2026 alias names REMOVED post-swap (2026-08-19): they are 301 redirects to the
    # -2027 boards (or, post-swap, to the community lists), so scanning them re-scanned
    # identical READMEs and DOUBLE-COUNTED every dead link under a phantom repo name
    # (101 rows reported for 63 unique dead). OUT-DEADLINK-SCAN-FP-1's rationale is
    # void: a redirect of the same content adds no detection coverage.
    # reference / evergreen
    "Research-Internships-for-Undergraduates", "underclassmen-internships",
    "resume-samples-2026", "interview-handbook-2026",
]

# URLs to skip (always OK or not worth checking)
SKIP_PATTERNS = [
    r'github\.com', r'img\.shields\.io', r'images/',
    r'raw\.githubusercontent\.com', r'badges\.gesis\.org',
    r'\.png$', r'\.jpg$', r'\.gif$', r'\.svg$',
    r'discord\.gg', r'discord\.com',
]
skip_re = re.compile('|'.join(SKIP_PATTERNS), re.IGNORECASE)

# Status codes we accept as "not dead"
ACCEPT_CODES = {200, 201, 202, 203, 204, 301, 302, 303, 307, 308, 403, 429}

# Soft-404 detection intentionally NOT implemented (OUT-SOFT404-SCAN-1, 2026-07-22).
# The one known soft-404 host (TikTok lifeattiktok) is IP-dependent: this GitHub runner
# gets HTTP 200 with a non-matching body, while users/other probes get a real 404 — so
# body-sniffing cannot catch it from here. Durable fix is upstream (AGG-TIKTOK-STALE-1
# purges the stale jobs). Do NOT re-add host body-sniffing without an IP-independent signature.

url_re = re.compile(r'https?://[^\s\)"<>\]]+')

def fetch_readme(repo):
    """Fetch README from raw GitHub."""
    # Try main first, then master (some repos use master)
    for branch in ('main', 'master'):
        try:
            url = f"https://raw.githubusercontent.com/zapplyjobs/{repo}/{branch}/README.md"
            req = urllib.request.Request(url, headers={"User-Agent": "out-deadlink/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except Exception:
            continue
    return None

def check_url(url, attempts=3):
    """Check a single URL. Returns (kind, value): kind 'code' (value=int HTTP status) or
    'transient' (value=error string after retries exhausted).
    OUT-DEADLINK-SCAN-FP-1: network errors (timeout / connection refused / DNS / reset)
    are retried with backoff; only after all retries fail are they 'transient' (NOT 'dead').
    4xx HTTP codes are definitive (returned immediately, no retry); 5xx server errors are retried like network errors (transient, not dead)."""
    last_err = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (out-deadlink/1.0)",
                "Accept": "text/html,application/json"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                return ('code', resp.status)
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600:
                # Server error (5xx) — transient like a timeout; retry before judging.
                last_err = f"HTTP {e.code}"
                if attempt < attempts - 1:
                    time.sleep(2 * (attempt + 1))  # backoff 2s, 4s
                continue
            return ('code', e.code)  # 4xx etc. — definitive response, no retry
        except Exception as e:
            last_err = str(e)[:100]
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))  # backoff 2s, 4s
    return ('transient', last_err)

all_urls = {}  # url → set of repos
for repo in repos:
    readme = fetch_readme(repo)
    if not readme:
        print(f"WARNING: Could not fetch README for {repo}")
        continue
    urls = set(url_re.findall(readme))
    filtered = {u for u in urls if not skip_re.search(u)}
    print(f"{repo}: {len(urls)} URLs found, {len(filtered)} to check")
    for u in filtered:
        all_urls.setdefault(u, set()).add(repo)

# ── OUT-LIFECYCLE-P4-MEMORY-1: carry-over with re-verification ────────────────
# The artifact was rebuilt daily from BOARD-VISIBLE urls only — but the consumer
# hides dead urls from the boards, so a hidden url drops out of the next scan's
# input, the artifact forgets it, and the publisher re-exposes it next cycle
# (verified 2026-08-22: 08-21 artifact 35 urls → boards clean → 08-22 artifact
# 0 urls → boards re-polluted same morning). Fix: load the previous artifact's
# ats-closed set, feed those urls through the SAME verdict paths (tenant listing
# / posting API / HTTP), and keep the ones still dead. Alive ⇒ dropped (job
# reopened — it may legitimately return to the boards). Unknown ⇒ retained with
# stale last_seen (proven closed before; an inconclusive re-check must not
# re-expose it); pruned at CARRY_MAX_AGE_DAYS, which covers the 14d pool TTL.
CARRY_MAX_AGE_DAYS = 16
prev_dead = {}  # url → {"repos": [...], "first_seen": iso}
try:
    _prev_src = os.environ.get("PREV_DEAD_LINKS_URL",
        "https://zjp-data-proxy.wild-queen-069e.workers.dev/data/dead-links.json")
    _req = urllib.request.Request(_prev_src,
        headers={'X-Proxy-Token': os.environ.get('DATA_PROXY_TOKEN', '')})
    with urllib.request.urlopen(_req, timeout=20) as _r:
        _prev = json.loads(_r.read())
    _prev_gen = _prev.get('generated_at') or ''
    for row in _prev.get('dead_links', []):
        u = row.get('url')
        if not isinstance(u, str) or row.get('http_code') != 'ats-closed':
            continue  # only the hideable class needs memory
        seen = row.get('first_seen') or _prev_gen
        try:
            _age = (time.time() - datetime.datetime.fromisoformat(seen).timestamp()) / 86400.0
        except Exception:
            _age = 0.0  # unparsable date ⇒ retain (safe; 16d prune still bounds it)
        if _age > CARRY_MAX_AGE_DAYS:
            continue
        e = prev_dead.setdefault(u, {"repos": [], "first_seen": seen})
        if row.get('repo'):
            e["repos"].append(row['repo'])
    print(f"Carried over {len(prev_dead)} ats-closed url(s) from previous artifact (max age {CARRY_MAX_AGE_DAYS}d)")
except Exception as e:
    print(f"Previous artifact unavailable (carry-over skipped — self-heals on a later run): {e}")

for u, e in prev_dead.items():
    _s = all_urls.setdefault(u, set())
    _s.update(r for r in e["repos"] if r and not r.startswith('('))
    if not _s:
        _s.add("(carried)")

print(f"\nTotal unique URLs to check: {len(all_urls)}")

# Cap at 6000 URLs (OUT-LIFECYCLE-P4-DISPLAY-1: the 14d display window roughly
# doubles the visible board set vs the old ~2700-at-7d; 6000 covers it. Was 3000.)
urls_to_check = list(all_urls.keys())
if len(urls_to_check) > 6000:
    print(f"Sampling 6000 of {len(urls_to_check)} URLs")
    urls_to_check = urls_to_check[:6000]

# Check URLs concurrently (15 at a time — raised from 10 for the larger 2026+2027 set)
results = {}
with ThreadPoolExecutor(max_workers=15) as pool:
    futures = {pool.submit(check_url, u): u for u in urls_to_check}
    for i, future in enumerate(as_completed(futures)):
        url = futures[future]
        results[url] = future.result()
        if (i + 1) % 100 == 0:
            print(f"  Checked {i+1}/{len(urls_to_check)}...")

# Classify: dead = definitive non-accepted HTTP code; transient = network failure after retries (not dead)
dead_links = []
transient_links = []
for url, (kind, value) in results.items():
    if kind == 'transient':
        for repo in all_urls[url]:
            transient_links.append({"repo": repo, "url": url, "http_code": value})
    elif value not in ACCEPT_CODES:
        for repo in all_urls[url]:
            dead_links.append({"repo": repo, "url": url, "http_code": value})
# ── ATS availability pass v2: tenant LISTING membership (OUT-DEADLINK-ATSIGNATURE-1) ──
# Workday/Ashby links return a 200 SPA shell whether the job is open or closed, so
# the status-code pass cannot judge them (the soft-404 blind class behind the
# recurring dev-team dead-link reports). v1 (2026-08-14) probed the per-job CXS
# DETAIL endpoint and was reverted to observability-only: per-job verdicts through
# the proxy are NON-DETERMINISTIC (same URL flips between jobPostingInfo and
# S21/S22/404/406 across requests/edges; Cox 406 storms for open jobs). Do NOT
# re-attempt per-job CXS closure verdicts.
# v2 (2026-08-15) uses the tenant job-LISTING (the same authoritative paginated
# POST AGG's workday fetcher consumes) through the authenticated wd-fetch-proxy:
#   POST {origin}/wday/cxs/{tenant}/{site}/jobs  {"limit":20,"offset":N}
#   → {"total": N, "jobPostings": [{"externalPath": "/job/.../Title_REQID"}]}
# Verdicts:
#   alive = reqId present in the listing (authoritative — this IS the site's data)
#   dead  = reqId absent AND the listing was enumerated to its full total AND
#           total <= ENUM_MAX_TOTAL (beyond-2000 tails are capped/unreachable —
#           SUP-verified: absence there proves nothing) — recorded to dead_links
#           with http_code 'ats-closed' + closed_via
#   unknown = capped tenant (total > ENUM_MAX_TOTAL), lookup-prefix miss,
#             enumeration error, or unparsable URL — recorded to ats_watch only
# Dry-run 2026-08-15: 131/131 small tenants enumerated 0-fail; 1445 board rows alive;
# 9 authoritative closures on tenants fully enumerated below the 2000 cap — 6/6 sampled
# (autodesk/cigna/gdit/kbr/hp/utaustin) browser-verified "page doesn't exist" at the user
# surface, and a listing-alive control rendered the live job. The 2 bpinternational
# reqIds absent from a full enumeration also returned S22 on 3/3 detail probes.
wd_re = re.compile(r'https://[a-z0-9-]+\.(?:wd\d*\.myworkdayjobs\.com|myworkdaysite\.com)/[^)\s"<\]]+')
ash_re = re.compile(r'https://jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)/([A-Za-z0-9-]+)')

PROXY = "https://wd-fetch-proxy.wild-queen-069e.workers.dev"
PROXY_TOKEN = os.environ.get('DATA_PROXY_TOKEN', '')
PAGE = 20
ENUM_MAX_TOTAL = 1999   # full enumeration is authoritative ONLY below 2000: the CXS `total`
                        # field itself caps at 2000 on big tenants (SUP-verified), and the
                        # unreachable tail can still hold OPEN jobs pushed down by inflow —
                        # absence there is NOT closure (same class AGG guards via carry-forward)
LOOKUP_PREFIX = 500     # capped tenants: enumerate only this many for positive membership

def wd_listing_post(key, offset):
    """POST the tenant listing through wd-fetch-proxy. Returns (status, dict|None)."""
    origin, tenant, site = key
    u = f"{origin}/wday/cxs/{tenant}/{site}/jobs"
    try:
        req = urllib.request.Request(f"{PROXY}/?url={urllib.parse.quote(u, safe='')}",
            method='POST',
            data=json.dumps({"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""}).encode(),
            headers={'X-Proxy-Token': PROXY_TOKEN, 'User-Agent': 'Mozilla/5.0 (out-deadlink/2.0)',
                     'Accept': 'application/json', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, None
    except Exception:
        return None, None

def reqid_of(segment):
    """Trailing _-suffixed id from a URL/externalPath last segment (Title_JR001 -> JR001)."""
    m = re.search(r'_([A-Za-z0-9-]+)$', segment)
    return m.group(1) if m else None

def wd_parse(u):
    """Board URL → (origin, tenant, site, reqId) or None."""
    m = re.match(r'(https://[^/]+)(/[^?#]*?)(?:[?#].*)?$', u)
    if not m: return None
    origin, path = m.groups()
    if 'myworkdaysite.com' in origin: return None  # tenant not derivable from hostname (AGG needs explicitTenant too)
    seg = [s for s in path.split('/') if s]
    if seg and re.match(r'^[a-z]{2}(-[A-Z]{2})?$', seg[0]): seg = seg[1:]
    if len(seg) < 2: return None
    tenant = origin.split('//')[1].split('.')[0]
    site = seg[0]
    rest = seg[1:]
    if rest and rest[0].lower() == site.lower(): rest = rest[1:]  # doubled-site (wellsfargojobs shape)
    if not rest: return None
    rid = reqid_of(rest[-1])
    if not rid: return None
    return (origin, tenant, site, rid)

ats_watch = []
ats_stats = {"judged": 0, "alive": 0, "dead": 0, "unknown": 0}
ats_dead_pending = []  # [(url, closed_via, note)] — flipped to dead_links below the storm guard

if not PROXY_TOKEN:
    # OUT-DEADLINK-CODECLEANUP-1: never silently degrade — an absent token must be
    # visible in logs AND in the published artifact (ats_stats.pass marker).
    print("\n⚠️ DATA_PROXY_TOKEN absent/empty — Workday ATS pass SKIPPED (ats_stats.pass marker set)")
    ats_stats["pass"] = "skipped-no-token"
wd_urls = sorted(u for u in all_urls if wd_re.search(u)) if PROXY_TOKEN else []
if wd_urls:
    print(f"\nATS listing-membership pass: {len(wd_urls)} Workday links")
    # key → {reqId → [urls]}; url → (key, reqId)
    key_reqs = {}
    for u in wd_urls:
        p = wd_parse(u)
        if not p:
            ats_stats["unknown"] += 1
            for repo in all_urls[u]:
                ats_watch.append({"repo": repo, "url": u, "note": "unparsable-wd-url"})
            continue
        key, reqid = (p[0], p[1], p[2]), p[3]
        key_reqs.setdefault(key, {}).setdefault(reqid, []).append(u)
    print(f"  distinct tenants: {len(key_reqs)}")

    def enumerate_key(key):
        """→ (mode, ids|None, total) — mode 'full'|'prefix'|'error'."""
        st, d = wd_listing_post(key, 0)
        if st != 200 or not isinstance(d, dict):
            return ('error', None, f'http-{st}')
        total = d.get('total') or 0
        if total == 0:
            return ('error', None, 'site-empty')  # posting site gone/renamed — not proof per-job
        ids = set()
        def grab(page):
            for p in page.get('jobPostings') or []:
                rid = reqid_of((p.get('externalPath') or '').split('/')[-1])
                if rid:
                    ids.add(rid)
                    ids.add(re.sub(r'-\d+$', '', rid))  # URL can carry a '-1' collision suffix the listing omits
        grab(d)
        limit = total if total <= ENUM_MAX_TOTAL else LOOKUP_PREFIX
        offset = PAGE
        misses = 0
        while offset < limit + PAGE:  # +PAGE margin: survive inserts shifting the tail mid-enumeration
            st2, d2 = wd_listing_post(key, offset)
            if st2 != 200 or not isinstance(d2, dict) or not d2.get('jobPostings'):
                misses += 1
                if misses >= 2:
                    if total <= ENUM_MAX_TOTAL:
                        return ('error', None, f'partial-{total}')  # gap → absence NOT authoritative
                    break  # prefix mode: keep what we have (positive membership still valid)
                time.sleep(2)
                continue
            misses = 0
            grab(d2)
            offset += PAGE
        return ('full' if total <= ENUM_MAX_TOTAL else 'prefix', ids, total)

    with ThreadPoolExecutor(max_workers=6) as pool2:
        enum_results = dict(zip(sorted(key_reqs), pool2.map(enumerate_key, sorted(key_reqs))))

    for key, (mode, ids, total) in enum_results.items():
        for reqid, urls in sorted(key_reqs[key].items()):
            for u in urls:
                # stats are per-URL (one verdict per unique board URL); dead_links rows
                # expand to per-repo later — the pending list carries each URL ONCE.
                ats_stats["judged"] += 1
                if ids is not None and reqid in ids:
                    ats_stats["alive"] += 1
                elif mode == 'full':
                    ats_stats["dead"] += 1
                    ats_dead_pending.append((u, 'wd-listing', f"absent from full listing (total={total})"))
                else:
                    ats_stats["unknown"] += 1
                    note = f"wd-{total}" if mode == 'error' else f"wd-capped-or-partial (total={total})"
                    for repo in all_urls[u]:
                        ats_watch.append({"repo": repo, "url": u, "note": note})
    print(f"  Workday verdicts: {ats_stats}")

# ── Ashby posting-API membership (same task; jobs.ashbyhq.com/{org}/{id}) ──
# GET https://api.ashbyhq.com/posting-api/job-board/{org} → {jobs:[{id,isListed,...}]}
# alive = id listed; dead = org board fetched OK but id absent or isListed=false;
# unknown on any fetch error (private/gated boards stay unknown).
ash_urls = sorted(u for u in all_urls if ash_re.search(u))  # posting API is public — no proxy token needed
if ash_urls:
    print(f"\nAshby posting-API pass: {len(ash_urls)} Ashby links")
    org_jobs = {}
    def ash_fetch(org):
        try:
            req = urllib.request.Request(f"https://api.ashbyhq.com/posting-api/job-board/{org}",
                headers={'User-Agent': 'Mozilla/5.0 (out-deadlink/2.0)', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return org, resp.status, json.loads(resp.read())
        except Exception as e:
            return org, None, str(e)[:60]
    orgs = sorted({ash_re.search(u).group(1) for u in ash_urls})
    with ThreadPoolExecutor(max_workers=4) as pool3:
        for org, st, d in pool3.map(ash_fetch, orgs):
            org_jobs[org] = (st, d)
    for u in ash_urls:
        m4 = ash_re.search(u)
        org, jid = m4.group(1), m4.group(2)
        st, d = org_jobs.get(org, (None, None))
        ats_stats["judged"] += 1
        if st == 200 and isinstance(d, dict) and isinstance(d.get('jobs'), list):
            match = [j for j in d['jobs'] if str(j.get('id')) == jid
                     or str(j.get('jobUrl') or '').rstrip('/').endswith('/' + jid)]
            if match and match[0].get('isListed') is not False:
                ats_stats["alive"] += 1
            else:
                ats_stats["dead"] += 1
                ats_dead_pending.append((u, 'ashby-posting-api', 'absent from org job board'))
        else:
            ats_stats["unknown"] += 1
            for repo in all_urls[u]:
                ats_watch.append({"repo": repo, "url": u, "note": f"ashby-board-error-{st}"})
    print(f"  Ashby+WD verdicts: {ats_stats}")

# Storm guard: a listing-side anomaly (proxy outage, WD behavior change) must not
# mass-close live jobs. Expected closure rate scales with display age: ~1.2% at
# ≤7d, measured 8.7% at 7-14d (out_deadzone_liveness.json 2026-08-21) — a 14d
# window blends to ~5-6%, so the cap is max(200, 12% of judged) (was max(25, 5%)).
storm_cap = max(200, int(ats_stats["judged"] * 0.12))
if len(ats_dead_pending) > storm_cap:
    print(f"  ⚠️ ATS storm guard: {len(ats_dead_pending)} deaths > cap {storm_cap} — NOT flipping to dead_links")
    for u, via, note in ats_dead_pending:
        for repo in all_urls[u]:
            ats_watch.append({"repo": repo, "url": u, "note": f"storm-guard-held ({via}, {note})"})
else:
    for u, via, note in ats_dead_pending:
        for repo in all_urls[u]:
            dead_links.append({"repo": repo, "url": u, "http_code": "ats-closed", "closed_via": via, "note": note})
    print(f"  ATS-closed links flipped to dead_links: {len(ats_dead_pending)} urls")

# ── OUT-LIFECYCLE-P4-MEMORY-1: retention + bookkeeping ───────────────────────
# Fresh dead rows get first_seen/last_seen; carried urls whose re-check was
# inconclusive (storm-held, capped tenant, fetch error → ats_watch) are RETAINED
# with their previous verdict; carried urls that re-verified ALIVE (or recorded
# no verdict shape at all) are dropped — the job may have reopened.
_today = now.date().isoformat()
_watch_urls = {w.get('url') for w in ats_watch}
_final_urls = {d['url'] for d in dead_links}
_retained = 0
for u, e in prev_dead.items():
    if u in _final_urls:
        continue
    if u in _watch_urls:
        for r in sorted(all_urls.get(u, {"(carried)"})):
            dead_links.append({"repo": r, "url": u, "http_code": "ats-closed",
                               "closed_via": "carry-over-unverified",
                               "note": "re-check inconclusive; retained from previous closure verdict"})
        _retained += 1
for d in dead_links:
    _e = prev_dead.get(d['url'])
    d['first_seen'] = _e['first_seen'] if _e else _today
    d['last_seen'] = _today
print(f"Carry-over: retained {_retained} unverified url(s); "
      f"{len(prev_dead) - _retained - len(_final_urls & set(prev_dead))} dropped (re-verified alive or no verdict)")


output = {
    "generated_at": now.isoformat(),
    "dead_links": dead_links,
    "total_checked": len(results),
    "total_dead": len(dead_links),
    "ats_watch": ats_watch,
    "ats_stats": ats_stats,
    "total_transient": len(transient_links),
    "transient_links": transient_links
}

# Print summary
print(f"\n=== Dead Link Check Results ===")
print(f"Total checked: {len(results)}")
print(f"Dead links: {len(dead_links)}")
print(f"Transient (network errors, NOT dead): {len(transient_links)}")
if dead_links:
    for dl in dead_links[:20]:
        print(f"  ❌ {dl['repo']}: {dl['url'][:80]} → {dl['http_code']}")
    if len(dead_links) > 20:
        print(f"  ... and {len(dead_links) - 20} more")
if transient_links:
    for tl in transient_links[:10]:
        print(f"  ⚠️  {tl['repo']}: {tl['url'][:80]} → {tl['http_code']}")
    if len(transient_links) > 10:
        print(f"  ... and {len(transient_links) - 10} more transient")

# Write step summary
summary = (f"## Dead Link Check\n\n- **Checked:** {len(results)} URLs\n- **Dead:** {len(dead_links)} links\n"
           f"- **Transient (not dead):** {len(transient_links)} links\n"
           f"- **ATS listing verdicts:** {ats_stats['judged']} judged — {ats_stats['alive']} alive, "
           f"{ats_stats['dead']} ats-closed, {ats_stats['unknown']} unknown (capped/error → ats_watch)\n")
with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
    f.write(summary)

# Publish to Supabase Storage (dual-write; R2 below is canonical)
try:
    _url = f"{os.environ.get('SUPABASE_URL', '')}/storage/v1/object/pipeline-data/dead-links.json"
    _req = urllib.request.Request(_url, method='PUT',
        data=json.dumps(output, indent=2).encode(),
        headers={'Authorization': f"Bearer {os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')}",
                 'apikey': os.environ.get('SUPABASE_SERVICE_ROLE_KEY', ''),
                 'Content-Type': 'application/json'})
    urllib.request.urlopen(_req, timeout=15)
    print("Results uploaded to Storage: pipeline-data/dead-links.json")
except Exception as e:
    print(f"Storage upload failed: {e}")
    sys.exit(1)

# R2 dual-write (recovery plan v2)
try:
    import boto3
    _s3 = boto3.client('s3',
        endpoint_url=os.environ.get('R2_ENDPOINT', ''),
        aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID', ''),
        aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY', ''))
    _s3.put_object(Bucket=os.environ.get('R2_BUCKET_NAME', 'zjp-data'),
                   Key='data/dead-links.json',
                   Body=json.dumps(output, indent=2).encode(),
                   ContentType='application/json')
    print("R2 upload OK: dead-links.json")
except Exception as e:
    print(f"R2 upload failed (non-blocking): {e}")

# Exit 1 only if truly-dead links found (transient excluded — triggers the tracking issue)
if dead_links:
    sys.exit(1)