#!/usr/bin/env python3
"""
OUT dead-link scanner (extracted from out-dead-link-check.yml heredoc 2026-08-16,
OUT-DEADLINK-CODECLEANUP-1 - same logic, lintable/testable as a file).

L3 scan redesign (2026-10-05): the scan universe is no longer README extraction but
the Supabase `jobs` table itself, read via PostgREST in posted_at bands:
  nightly: 0-2d FULL + one 1-day bucket of the 2-7d window (rotation, comment below)
  deep (UTC Saturdays only): 0-30d FULL
The HTTP pass is per-host polite (400/1200 per-host cap, concurrency 3, 0.5s spacing,
403/429 circuit breaker) and a URL dies only on its SECOND 404/410 sighting:
http_sightings persists observations across runs, so first-sight 404s are no longer
tombstones and ambiguous codes (403/429/401/405/451/final-5xx) can never kill a URL
(fail-open preserved). ATS availability passes (Workday tenant-listing membership via
wd-fetch-proxy; Ashby posting API) are unchanged, now fed from the band universe.
Publishes dead-links.json to Supabase Storage + R2 (data/ prefix, canonical).
Exit codes: 0 = ran and published (findings are DATA - OUT-DEADLINK-EXITCODE-ALERTCONV-1);
1 = scan/publish error; 2 = universe fetch failed/empty on a nightly run (loud,
nothing published - last-good artifact preserved).
Output schema (additive vs pre-L3): {generated_at, total_checked, total_dead,
 dead_links:[{repo,url,http_code,closed_via?,note?,first_seen?,last_seen?}],
 ats_watch:[...], ats_stats:{judged,alive,dead,unknown,(pass?)},
 total_transient, transient_links,
 http_sightings:{url:{code,first_seen,last_seen,sightings}},
 scan_scope:{mode,bands,band_b_selected,requested,after_host_caps,checked,host_skipped,generated_under}}
"""
import json, urllib.request, urllib.error, urllib.parse, re, os, sys, datetime, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Semaphore
from collections import Counter


now = datetime.datetime.now(datetime.timezone.utc)
DRYRUN = os.environ.get('SCAN_DRYRUN') == '1'
# Local dry-run helper: consume a pre-fetched universe array [{job_url,source_id,posted_at}]
# instead of PostgREST. Production never sets this.
UNIVERSE_FILE = os.environ.get('SCAN_UNIVERSE_FILE', '')
# Deep pass is decided in-script by UTC day-of-week (Monday=0 ... Saturday=5): the
# workflow schedule stays daily 06:00 UTC; only Saturday runs take the 30d band.
SCAN_MODE = 'deep' if now.weekday() == 5 else 'nightly'
TODAY = now.date().isoformat()

# URLs to skip (always OK or not worth checking)
SKIP_PATTERNS = [
    r'github\.com', r'img\.shields\.io', r'images/',
    r'raw\.githubusercontent\.com', r'badges\.gesis\.org',
    r'\.png$', r'\.jpg$', r'\.gif$', r'\.svg$',
    r'discord\.gg', r'discord\.com',
]
skip_re = re.compile('|'.join(SKIP_PATTERNS), re.IGNORECASE)

# Status codes we ACCEPT as "not dead" - an accepted final code CLEARS the URL from
# http_sightings (the recovery path). 403/429 were flatly ACCEPT pre-L3 (fail-open);
# they stay fail-open but are now recorded as ambiguous sightings (never kill, never
# erase prior sightings) - see the N-observation classification below.
ACCEPT_CODES = {200, 201, 202, 203, 204, 301, 302, 303, 307, 308}

# Soft-404 detection intentionally NOT implemented (OUT-SOFT404-SCAN-1, 2026-07-22).
# The one known soft-404 host (TikTok lifeattiktok) is IP-dependent: this GitHub runner
# gets HTTP 200 with a non-matching body, while users/other probes get a real 404 - so
# body-sniffing cannot catch it from here. Durable fix is upstream (AGG-TIKTOK-STALE-1
# purges the stale jobs). Do NOT re-add host body-sniffing without an IP-independent signature.

# ── L3 universe: Supabase PostgREST band reads (replaces README extraction) ───
# public.jobs via PostgREST: is_active=eq.true AND source=eq.GITHUB + posted_at
# windows per band. idx_jobs_active (is_active, posted_at DESC) WHERE is_active=true
# makes these ordered range scans cheap. Creds: SUPABASE_URL +
# SUPABASE_SERVICE_ROLE_KEY (repo secrets in Actions; local runs fall back to
# ~/.secrets/supabase-zjp.env).
REST_PAGE = 1000                               # Range-header pagination, 1000/page
HARD_CAP = {'nightly': 25000, 'deep': 100000}  # safety caps (replace the old flat 6000)
PER_HOST_CAP = {'nightly': 400, 'deep': 1200}  # politeness caps, oldest-first per host

def _sb_creds():
    url = os.environ.get('SUPABASE_URL', '')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if url and key:
        return url.rstrip('/'), key
    env_path = os.path.expanduser('~/.secrets/supabase-zjp.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = (s.strip().strip('"').strip("'") for s in line.split('=', 1))
                if k == 'SUPABASE_URL' and not url:
                    url = v
                elif k == 'SUPABASE_SERVICE_ROLE_KEY' and not key:
                    key = v
    return url.rstrip('/'), key

def rest_fetch_band(posted_gte, posted_lt=None):
    """Page one posted_at band out of public.jobs (job_url, source_id, posted_at),
    ordered posted_at.asc, 1000 rows/page via the Range header."""
    sb, key = _sb_creds()
    if not sb or not key:
        raise RuntimeError('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not available (env or ~/.secrets/supabase-zjp.env)')
    q = (f"{sb}/rest/v1/jobs?select=job_url,source_id,posted_at"
         f"&is_active=eq.true&source=eq.GITHUB&order=posted_at.asc"
         f"&posted_at=gte.{urllib.parse.quote(posted_gte, safe='')}")
    if posted_lt:
        q += f"&posted_at=lt.{urllib.parse.quote(posted_lt, safe='')}"
    rows, offset = [], 0
    while True:
        req = urllib.request.Request(q, headers={
            'apikey': key, 'Authorization': f'Bearer {key}',
            'Prefer': 'count=none', 'Range': f'{offset}-{offset + REST_PAGE - 1}'})
        with urllib.request.urlopen(req, timeout=30) as r:
            page = json.loads(r.read())
        if not isinstance(page, list):
            raise ValueError(f'unexpected PostgREST payload at offset {offset} (non-list)')
        rows.extend(page)
        if len(page) < REST_PAGE:
            return rows
        offset += REST_PAGE

def _iso_days_ago(days):
    return (now - datetime.timedelta(days=days)).isoformat()

def fetch_universe():
    """-> (rows, bands_counts, band_b_selected). Nightly: band A (0-2d FULL) + band B
    (one 1-day bucket of 2-7d). Deep (Saturdays): 0-30d FULL. Raises on band-A failure."""
    bands, band_b_selected = {}, None
    band_a = rest_fetch_band(_iso_days_ago(2))
    bands['a_lt_2d'] = len(band_a)
    if SCAN_MODE == 'deep':
        band_b = rest_fetch_band(_iso_days_ago(30))
        bands['deep_lt_30d'] = len(band_b)
    else:
        # Band-B rotation: the 2-7d window is five 1-day buckets keyed by floor(age_days)
        # in {2,3,4,5,6}. Bucket index = (UTC day-of-year) % 5, mapping residues 0..4 ->
        # buckets[0..4] (e.g. 2026-10-05 = doy 278 -> 278%5=3 -> the 5d bucket). Each UTC
        # day picks exactly one bucket; residues cycle all five values every 5 days, so
        # the full 2-7d window is covered once per 5 days while the 0-2d band is
        # re-verified every run. Deterministic - same UTC day gives the same bucket.
        buckets = [2, 3, 4, 5, 6]
        band_b_selected = buckets[now.timetuple().tm_yday % 5]
        try:
            band_b = rest_fetch_band(_iso_days_ago(band_b_selected + 1), _iso_days_ago(band_b_selected))
            bands[f'b_2_7d_bucket_{band_b_selected}d'] = len(band_b)
        except Exception as e:
            # Band A already verified non-empty -> degrade loudly to band A only.
            print(f"WARNING: band B ({band_b_selected}d bucket) fetch failed - continuing with band A only: {e}")
            band_b = []
    return band_a + band_b, bands, band_b_selected

def universe_from_file(path):
    """SCAN_UNIVERSE_FILE loader: [{job_url,source_id,posted_at}] -> universe rows."""
    with open(path) as f:
        raw = json.load(f)
    rows = []
    for r in raw:
        u = r.get('job_url')
        if not u:
            continue
        rows.append({'job_url': u, 'source_id': r.get('source_id'), 'posted_at': r.get('posted_at')})
    return rows

def check_url(url, attempts=3):
    """Check a single URL. Returns (kind, value): kind 'code' (value=int HTTP status) or
    'transient' (value=error string after retries exhausted).
    OUT-DEADLINK-SCAN-FP-1: network errors (timeout / connection refused / DNS / reset)
    are retried with backoff; only after all retries fail are they 'transient' (NOT 'dead').
    4xx HTTP codes are definitive (returned immediately, no retry); 5xx server errors are retried like network errors (transient, not dead)."""
    # OUT-DEADLINK-CLICKSELFPOP-1 (2026-10-02): tracked links carry ?s=<source>
    # attribution. Checking them verbatim logged one clicks-row per link per run into
    # per-source analytics buckets (32,713 self-pollution rows by 09-28,
    # INF-CLICKS-BOTPOLLUTION-1 investigation). The redirect route reads the job from
    # the path only, so stripping the query leaves the landing page and the verdict
    # unchanged while the rows we do create stay attributable via UA (out-deadlink/*).
    if '/l/d/' in url:
        url = url.split('?', 1)[0]
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
                # Server error (5xx) - transient like a timeout; retry before judging.
                last_err = f"HTTP {e.code}"
                if attempt < attempts - 1:
                    time.sleep(2 * (attempt + 1))  # backoff 2s, 4s
                continue
            return ('code', e.code)  # 4xx etc. - definitive response, no retry
        except Exception as e:
            last_err = str(e)[:100]
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))  # backoff 2s, 4s
    return ('transient', last_err)

# ── Build the scan universe ───────────────────────────────────────────────────
if UNIVERSE_FILE:
    print(f"SCAN_UNIVERSE_FILE set - universe from {UNIVERSE_FILE} (local dry-run mode)")
    try:
        universe_rows = universe_from_file(UNIVERSE_FILE)
        bands = {'universe_file': len(universe_rows)}
        band_b_selected = None
    except Exception as e:
        print(f"FATAL: unreadable SCAN_UNIVERSE_FILE: {e}\n  publishing NOTHING, exit 2")
        sys.exit(2)
else:
    try:
        universe_rows, bands, band_b_selected = fetch_universe()
    except Exception as e:
        # Failsafe (L3): a broken band fetch on a nightly run must never scan an empty
        # or partial universe silently - publish nothing, exit 2, last-good artifact survives.
        print(f"FATAL: universe band fetch failed ({SCAN_MODE}): {e}\n  publishing NOTHING, exit 2 (last-good artifact preserved)")
        sys.exit(2)
    if SCAN_MODE == 'nightly' and bands.get('a_lt_2d', 0) == 0:
        # Failsafe (L3): band A (0-2d) is re-verified EVERY nightly run - 0 rows means
        # the pipeline/star schema broke, not that no jobs exist.
        print("FATAL: nightly band A (posted_at >= now()-2d, is_active, GITHUB) returned 0 rows\n  publishing NOTHING, exit 2 (last-good artifact preserved)")
        sys.exit(2)

# Dedupe by job_url keeping the OLDEST posted_at (rows arrive posted_at.asc, so the
# first occurrence wins; stable source_id tiebreak in the sort key), drop null/empty
# job_url (none today - guard anyway), apply SKIP_PATTERNS.
def _posted_key(row):
    s = row.get('posted_at')
    try:
        d = datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        d = None
    return (0 if d else 1, d or now, str(row.get('source_id') or ''))

def host_of(u):
    return urllib.parse.urlsplit(u).netloc.lower()

seen_urls = set()
universe = []  # oldest-first: (posted_at asc, source_id asc)
for row in sorted(universe_rows, key=_posted_key):
    u = row.get('job_url')
    if not u or u in seen_urls or skip_re.search(u):
        continue
    seen_urls.add(u)
    universe.append(row)

requested = len(universe)
_hard = HARD_CAP[SCAN_MODE]
if requested > _hard:
    # L3 safety cap (replaces the flat 6000 + its stale 14d-display-window rationale:
    # the universe is now the DB band itself, so the cap only guards runaway tables -
    # the per-host caps normally bound the check list first). Oldest survive.
    print(f"HARD CAP: {requested} universe URLs > {_hard} ({SCAN_MODE}) - truncating to the oldest {_hard}")
    universe = universe[:_hard]
    requested = len(universe)

# Per-host politeness caps (HTTP pass): keep OLDEST posted_at first (universe is
# oldest-first), nightly 400 / deep 1200 per host - one huge tenant must not
# dominate the run.
check_rows = []
_host_n = Counter()
for row in universe:
    row['_host'] = host_of(row['job_url'])
    if _host_n[row['_host']] >= PER_HOST_CAP[SCAN_MODE]:
        continue
    _host_n[row['_host']] += 1
    check_rows.append(row)

# ── OUT-LIFECYCLE-P4-MEMORY-1: carry-over with re-verification ────────────────
# The artifact was rebuilt daily from BOARD-VISIBLE urls only - but the consumer
# hides dead urls from the boards, so a hidden url drops out of the next scan's
# input, the artifact forgets it, and the publisher re-exposes it next cycle
# (verified 2026-08-22: 08-21 artifact 35 urls -> boards clean -> 08-22 artifact
# 0 urls -> boards re-polluted same morning). Fix: load the previous artifact's
# ats-closed set, feed those urls through the SAME verdict paths (tenant listing
# / posting API / HTTP), and keep the ones still dead. Alive => dropped (job
# reopened - it may legitimately return to the boards). Unknown => retained with
# stale last_seen (proven closed before; an inconclusive re-check must not
# re-expose it); pruned at CARRY_MAX_AGE_DAYS.
CARRY_MAX_AGE_DAYS = 35  # was 16 (README-universe era: covered the 14d pool TTL).
                         # Under band rotation the deepest scan reaches 30d, so carried
                         # ats-closed rows must outlive a full deep cycle -> 35d.
prev_dead = {}        # url -> {"repos": [...], "first_seen": iso}   (ats-closed class only)
http_sightings = {}   # url -> {code, first_seen, last_seen, sightings}  (L3 N-observation memory)
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
            continue  # only the hideable class needs ats-closed memory
        seen = row.get('first_seen') or _prev_gen
        try:
            _age = (time.time() - datetime.datetime.fromisoformat(seen).timestamp()) / 86400.0
        except Exception:
            _age = 0.0  # unparsable date => retain (safe; prune still bounds it)
        if _age > CARRY_MAX_AGE_DAYS:
            continue
        e = prev_dead.setdefault(u, {"repos": [], "first_seen": seen})
        if row.get('repo'):
            e["repos"].append(row['repo'])
    for u, s in (_prev.get('http_sightings') or {}).items():
        if not isinstance(u, str) or not isinstance(s, dict):
            continue
        http_sightings[u] = {
            'code': s.get('code'),
            'first_seen': s.get('first_seen') or TODAY,
            'last_seen': s.get('last_seen') or TODAY,
            'sightings': int(s.get('sightings') or 0),
        }
    print(f"Carried over {len(prev_dead)} ats-closed url(s) + {len(http_sightings)} http-sighting url(s) from previous artifact (max age {CARRY_MAX_AGE_DAYS}d)")
except Exception as e:
    print(f"Previous artifact unavailable (carry-over skipped - self-heals on a later run): {e}")

# Carried ats-closed urls re-verify through the SAME verdict paths regardless of band
# rotation (they are usually older than the deepest band). Appended AFTER the per-host
# caps: they are few (bounded by the storm guard / CARRY_MAX_AGE_DAYS) and their
# hosts already passed politeness this run.
for u, e in prev_dead.items():
    if u in seen_urls:
        continue  # still band-visible - checked once, as a universe row
    seen_urls.add(u)
    check_rows.append({'job_url': u, 'posted_at': None, '_host': host_of(u),
                       'source_id': e["repos"][0] if e["repos"] else '(carried)'})

after_host_caps = len(check_rows)

# ── Per-host politeness (HTTP pass only) ─────────────────────────────────────
# Max 3 concurrent requests per host + >=0.5s spacing between same-host requests.
# Circuit breaker: 10 consecutive 403/429 on one host -> skip that host's remaining
# URLs this run (counted in scan_scope.host_skipped, NEVER classified dead - the
# host is blocking us, which proves nothing about the jobs).
host_state = {}

def _hstate(host):
    st = host_state.get(host)
    if st is None:
        st = host_state[host] = {'sem': Semaphore(3), 'lock': Lock(),
                                 'next_ok': 0.0, 'consec': 0, 'broken': False}
    return st

def polite_check(url):
    host = host_of(url)
    st = _hstate(host)
    if st['broken']:
        return ('host-skipped', None)
    with st['sem']:
        if st['broken']:  # breaker may have tripped while queued on the semaphore
            return ('host-skipped', None)
        with st['lock']:
            delay = st['next_ok'] - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            st['next_ok'] = time.monotonic() + 0.5
        kind, value = check_url(url)
    if kind == 'code':
        with st['lock']:
            if value in (403, 429):
                st['consec'] += 1
                if st['consec'] >= 10:
                    st['broken'] = True
                    print(f"  circuit breaker: {host} after 10 consecutive 403/429 - skipping its remaining URLs this run")
            else:
                st['consec'] = 0
    return (kind, value)

print(f"\nScan mode: {SCAN_MODE} | bands: {bands} | band_b_selected: {band_b_selected}")
print(f"Universe: {requested} requested, {after_host_caps} after per-host caps "
      f"({PER_HOST_CAP[SCAN_MODE]}/host) + carried; distinct hosts: {len(_host_n)}")

if DRYRUN:
    print("\n=== DRY-RUN universe plan ===")
    print(f"mode: {SCAN_MODE} | bands: {bands} | band_b_selected: {band_b_selected}")
    print(f"universe: {len(universe_rows)} raw rows -> {requested} after dedupe/skip/caps")
    print("top-10 hosts by URL count:")
    for h, n in Counter(row['_host'] for row in check_rows).most_common(10):
        print(f"  {n:5d}  {h}")
    if len(check_rows) > 50:
        # Dry run: at most 50 URLs, spread across hosts (round-robin, oldest-first per host).
        by_host = {}
        for row in check_rows:
            by_host.setdefault(row['_host'], []).append(row)
        sampled = []
        while len(sampled) < 50:
            progressed = False
            for h in sorted(by_host):
                if by_host[h] and len(sampled) < 50:
                    sampled.append(by_host[h].pop(0))
                    progressed = True
            if not progressed:
                break
        print(f"DRY-RUN: sampling {len(sampled)} of {len(check_rows)} URLs (spread across {len(by_host)} hosts)")
        check_rows = sampled

print(f"\nTotal unique URLs to check: {len(check_rows)}")

url_src = {row['job_url']: (row.get('source_id') or '(carried)') for row in check_rows}

results = {}
host_skipped = 0
with ThreadPoolExecutor(max_workers=15) as pool:
    futures = {pool.submit(polite_check, row['job_url']): row['job_url'] for row in check_rows}
    for i, future in enumerate(as_completed(futures)):
        url = futures[future]
        kind, value = future.result()
        if kind == 'host-skipped':
            host_skipped += 1
        else:
            results[url] = (kind, value)
        if (i + 1) % 100 == 0:
            print(f"  Checked {i+1}/{len(check_rows)}...")
print(f"  host-skipped (circuit breaker): {host_skipped}")

# ── L3 N-observation classification (http_sightings) ─────────────────────────
# Death requires TWO 404/410 sightings across runs (pre-L3, a first-sight 404 was
# published as a tombstone immediately - the flaw this fixes). Ambiguous codes
# (403/429/401/405/451/final-5xx) are recorded but NEVER kill (preserves the
# fail-open) and never erase prior sightings - a bot-block must not reset death
# evidence. Accepted codes clear the URL (recovery). 5xx-final and pure network
# failures stay 'transient' (never dead); only code-bearing outcomes update sightings.
DEATH_CODES = {404, 410}

def _record_sighting(url, code, death_candidate):
    s = http_sightings.get(url)
    if s is None:
        s = http_sightings[url] = {'code': code, 'first_seen': TODAY, 'last_seen': TODAY, 'sightings': 0}
    if death_candidate:
        s['sightings'] = int(s.get('sightings') or 0) + 1
        s['code'] = code
        s['last_seen'] = TODAY
    elif int(s.get('sightings') or 0) >= 2:
        s['last_seen'] = TODAY  # already a confirmed death: keep the killing code visible
    else:
        s['code'] = code
        s['last_seen'] = TODAY

_class_counts = Counter()
for url, (kind, value) in results.items():
    if kind == 'transient':
        m = re.fullmatch(r'HTTP (\d{3})', str(value))
        if m:
            _record_sighting(url, int(m.group(1)), death_candidate=False)  # 5xx final
            _class_counts['transient-5xx'] += 1
        else:
            _class_counts['transient-network'] += 1  # no code - no sighting change
    elif value in ACCEPT_CODES:
        http_sightings.pop(url, None)  # recovery path
        _class_counts['accepted'] += 1
    elif value in DEATH_CODES:
        _record_sighting(url, value, death_candidate=True)
        _class_counts['death-candidate'] += 1
    else:
        _record_sighting(url, value, death_candidate=False)  # 403/429/401/405/451/...
        _class_counts['ambiguous'] += 1

# HTTP deaths = every URL with >=2 sightings that was NOT accepted-cleared this run.
# Includes confirmed urls the rotation did not re-check - without this they would
# drop out of dead_links and the publisher would re-expose them (the exact
# OUT-LIFECYCLE-P4-MEMORY-1 failure mode, now closed for the http class too).
http_dead_urls = {u for u, s in http_sightings.items() if int(s.get('sightings') or 0) >= 2}

dead_links = []
transient_links = []
for url, (kind, value) in results.items():
    if kind == 'transient':
        transient_links.append({"repo": url_src.get(url, '(carried)'), "url": url, "http_code": value})
for u in sorted(http_dead_urls):
    s = http_sightings[u]
    try:
        _code = int(s.get('code'))
    except Exception:
        _code = 404
    # repo = the row's source_id: the board mapping is not available DB-side, and
    # source_id stays traceable to the public.jobs row. DASH consumer check: `repo`
    # is treated as an opaque label today - verify before ever joining on it.
    dead_links.append({"repo": url_src.get(u, '(out-of-band)'), "url": u, "http_code": _code})

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
#   -> {"total": N, "jobPostings": [{"externalPath": "/job/.../Title_REQID"}]}
# Verdicts:
#   alive = reqId present in the listing (authoritative - this IS the site's data)
#   dead  = reqId absent AND the listing was enumerated to its full total AND
#           total <= ENUM_MAX_TOTAL (beyond-2000 tails are capped/unreachable -
#           SUP-verified: absence there proves nothing) - recorded to dead_links
#           with http_code 'ats-closed' + closed_via
#   unknown = capped tenant (total > ENUM_MAX_TOTAL), lookup-prefix miss,
#             enumeration error, or unparsable URL - recorded to ats_watch only
# Dry-run 2026-08-15: 131/131 small tenants enumerated 0-fail; 1445 board rows alive;
# 9 authoritative closures on tenants fully enumerated below the 2000 cap - 6/6 sampled
# (autodesk/cigna/gdit/kbr/hp/utaustin) browser-verified "page doesn't exist" at the
# user surface, and a listing-alive control rendered the live job. The 2 bpinternational
# reqIds absent from a full enumeration also returned S22 on 3/3 detail probes.
wd_re = re.compile(r'https://[a-z0-9-]+\.(?:wd\d*\.myworkdayjobs\.com|myworkdaysite\.com)/[^)\s"<\]]+')
ash_re = re.compile(r'https://jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)/([A-Za-z0-9-]+)')

PROXY = "https://wd-fetch-proxy.wild-queen-069e.workers.dev"
PROXY_TOKEN = os.environ.get('DATA_PROXY_TOKEN', '')
PAGE = 20
ENUM_MAX_TOTAL = 1999   # full enumeration is authoritative ONLY below 2000: the CXS `total`
                        # field itself caps at 2000 on big tenants (SUP-verified), and the
                        # unreachable tail can still hold OPEN jobs pushed down by inflow -
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
    """Board URL -> (origin, tenant, site, reqId) or None."""
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
ats_dead_pending = []  # [(url, closed_via, note)] - flipped to dead_links below the storm guard

if not PROXY_TOKEN:
    # OUT-DEADLINK-CODECLEANUP-1: never silently degrade - an absent token must be
    # visible in logs AND in the published artifact (ats_stats.pass marker).
    print("\nDATA_PROXY_TOKEN absent/empty - Workday ATS pass SKIPPED (ats_stats.pass marker set)")
    ats_stats["pass"] = "skipped-no-token"
wd_urls = sorted(u for u in url_src if wd_re.search(u)) if PROXY_TOKEN else []
if wd_urls:
    print(f"\nATS listing-membership pass: {len(wd_urls)} Workday links")
    # key -> {reqId -> [urls]}
    key_reqs = {}
    for u in wd_urls:
        p = wd_parse(u)
        if not p:
            ats_stats["unknown"] += 1
            ats_watch.append({"repo": url_src.get(u, '(carried)'), "url": u, "note": "unparsable-wd-url"})
            continue
        key, reqid = (p[0], p[1], p[2]), p[3]
        key_reqs.setdefault(key, {}).setdefault(reqid, []).append(u)
    print(f"  distinct tenants: {len(key_reqs)}")

    def enumerate_key(key):
        """-> (mode, ids|None, total) - mode 'full'|'prefix'|'error'."""
        st, d = wd_listing_post(key, 0)
        if st != 200 or not isinstance(d, dict):
            return ('error', None, f'http-{st}')
        total = d.get('total') or 0
        if total == 0:
            return ('error', None, 'site-empty')  # posting site gone/renamed - not proof per-job
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
                        return ('error', None, f'partial-{total}')  # gap -> absence NOT authoritative
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
                # carry the url's source_id below - the pending list holds each URL ONCE.
                ats_stats["judged"] += 1
                if ids is not None and reqid in ids:
                    ats_stats["alive"] += 1
                elif mode == 'full':
                    ats_stats["dead"] += 1
                    ats_dead_pending.append((u, 'wd-listing', f"absent from full listing (total={total})"))
                else:
                    ats_stats["unknown"] += 1
                    note = f"wd-{total}" if mode == 'error' else f"wd-capped-or-partial (total={total})"
                    ats_watch.append({"repo": url_src.get(u, '(carried)'), "url": u, "note": note})
    print(f"  Workday verdicts: {ats_stats}")

# ── Ashby posting-API membership (same task; jobs.ashbyhq.com/{org}/{id}) ──
# GET https://api.ashbyhq.com/posting-api/job-board/{org} -> {jobs:[{id,isListed,...}]}
# alive = id listed; dead = org board fetched OK but id absent or isListed=false;
# unknown on any fetch error (private/gated boards stay unknown).
ash_urls = sorted(u for u in url_src if ash_re.search(u))  # posting API is public - no proxy token needed
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
            ats_watch.append({"repo": url_src.get(u, '(carried)'), "url": u, "note": f"ashby-board-error-{st}"})
    print(f"  Ashby+WD verdicts: {ats_stats}")

# Storm guard: a listing-side anomaly (proxy outage, WD behavior change) must not
# mass-close live jobs. Expected closure rate scales with display age: ~1.2% at
# <=7d, measured 8.7% at 7-14d (out_deadzone_liveness.json 2026-08-21) - a 14d
# window blends to ~5-6%, so the cap is max(200, 12% of judged) (was max(25, 5%)).
# L3: additionally bounded at 499 - the backend's DEAD_URL_FLIP_MAX=500 transport
# is all-or-nothing, so breaching 500 would drop the WHOLE batch; overflow rows
# park in ats_watch instead of ever being published as dead.
storm_cap = min(max(200, int(ats_stats["judged"] * 0.12)), 499)
if len(ats_dead_pending) > storm_cap:
    print(f"  ATS storm guard: {len(ats_dead_pending)} deaths > cap {storm_cap} - NOT flipping to dead_links")
    for u, via, note in ats_dead_pending:
        ats_watch.append({"repo": url_src.get(u, '(carried)'), "url": u, "note": f"storm-guard-held ({via}, {note})"})
else:
    for u, via, note in ats_dead_pending:
        dead_links.append({"repo": url_src.get(u, '(carried)'), "url": u, "http_code": "ats-closed", "closed_via": via, "note": note})
    print(f"  ATS-closed links flipped to dead_links: {len(ats_dead_pending)} urls")

# ── OUT-LIFECYCLE-P4-MEMORY-1: retention + bookkeeping ───────────────────────
# Fresh dead rows get first_seen/last_seen; carried urls whose re-check was
# inconclusive (storm-held, capped tenant, fetch error -> ats_watch) are RETAINED
# with their previous verdict; carried urls that re-verified ALIVE (or recorded
# no verdict shape at all) are dropped - the job may have reopened.
_today = TODAY
_watch_urls = {w.get('url') for w in ats_watch}
_final_urls = {d['url'] for d in dead_links}
_retained = 0
for u, e in prev_dead.items():
    if u in _final_urls:
        continue
    if u in _watch_urls:
        dead_links.append({"repo": url_src.get(u, '(carried)'), "url": u, "http_code": "ats-closed",
                           "closed_via": "carry-over-unverified",
                           "note": "re-check inconclusive; retained from previous closure verdict"})
        _retained += 1
for d in dead_links:
    _s = http_sightings.get(d['url']) or {}
    _e = prev_dead.get(d['url']) or {}
    d['first_seen'] = _e.get('first_seen') or _s.get('first_seen') or _today
    d['last_seen'] = _today
print(f"Carry-over: retained {_retained} unverified url(s); "
      f"{len(prev_dead) - _retained - len(_final_urls & set(prev_dead))} dropped (re-verified alive or no verdict)")

# Prune http_sightings entries gone stale beyond the carry horizon. Rotation means
# absent != recovered (only an accepted code clears) - the prune only bounds growth.
_pruned = 0
for u in list(http_sightings):
    s = http_sightings[u]
    try:
        _age = (time.time() - datetime.datetime.fromisoformat(str(s.get('last_seen') or s.get('first_seen') or TODAY)).timestamp()) / 86400.0
    except Exception:
        continue
    if _age > CARRY_MAX_AGE_DAYS:
        del http_sightings[u]
        _pruned += 1
if _pruned:
    print(f"http_sightings: pruned {_pruned} entry(ies) older than {CARRY_MAX_AGE_DAYS}d")

scan_scope = {
    "mode": SCAN_MODE,
    "bands": bands,
    "band_b_selected": band_b_selected,
    "requested": requested,
    "after_host_caps": after_host_caps,
    "checked": len(results),
    "host_skipped": host_skipped,
    "generated_under": "out-l3-scan/1",
}

output = {
    "generated_at": now.isoformat(),
    "dead_links": dead_links,
    "total_checked": len(results),
    "total_dead": len(dead_links),
    "ats_watch": ats_watch,
    "ats_stats": ats_stats,
    "total_transient": len(transient_links),
    "transient_links": transient_links,
    "http_sightings": http_sightings,
    "scan_scope": scan_scope,
}

# Print summary
print(f"\n=== Dead Link Check Results ===")
print(f"Scan mode: {SCAN_MODE} | requested: {requested} | after host caps: {after_host_caps} | checked: {len(results)} | host-skipped: {host_skipped}")
print(f"Total checked: {len(results)}")
print(f"Dead links: {len(dead_links)}")
print(f"Transient (network errors, NOT dead): {len(transient_links)}")
print(f"http_sightings: {len(http_sightings)} entry(ies) | confirmed-dead (sightings>=2): {len(http_dead_urls)}")
if DRYRUN:
    print(f"DRY-RUN classification breakdown: {dict(_class_counts)}")
if dead_links:
    for dl in dead_links[:20]:
        print(f"  X {dl['repo']}: {dl['url'][:80]} -> {dl['http_code']}")
    if len(dead_links) > 20:
        print(f"  ... and {len(dead_links) - 20} more")
if transient_links:
    for tl in transient_links[:10]:
        print(f"  ~ {tl['repo']}: {tl['url'][:80]} -> {tl['http_code']}")
    if len(transient_links) > 10:
        print(f"  ... and {len(transient_links) - 10} more transient")

# Write step summary
summary = (f"## Dead Link Check\n\n- **Checked:** {len(results)} URLs\n- **Dead:** {len(dead_links)} links\n"
           f"- **Transient (not dead):** {len(transient_links)} links\n"
           f"- **ATS listing verdicts:** {ats_stats['judged']} judged - {ats_stats['alive']} alive, "
           f"{ats_stats['dead']} ats-closed, {ats_stats['unknown']} unknown (capped/error -> ats_watch)\n"
           f"- **Scan scope:** {SCAN_MODE} mode, {requested} requested / {after_host_caps} after host caps, "
           f"{host_skipped} host-skipped\n")
with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
    f.write(summary)

if DRYRUN:
    print("\n(dry-run) SCAN_DRYRUN=1 - publish to Storage/R2 and scan_summary.json SKIPPED; exiting 0 regardless of findings")
    sys.exit(0)

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

# OUT-DEADLINK-EXITCODE-ALERTCONV-1 (2026-10-04): findings are DATA, not failure.
# The scan exits 0 when it ran and published, whether or not dead links were
# found; the workflow reads scan_summary.json to alert on findings. Exit 1
# stays reserved for scan/publish errors (Storage failure above).
with open('scan_summary.json', 'w') as _sf:
    json.dump(output, _sf, indent=2)
if dead_links:
    print(f"Dead links found: {len(dead_links)} (written to scan_summary.json; exit 0 - findings alert is the workflow's job)")
