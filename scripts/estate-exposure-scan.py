#!/usr/bin/env python3
"""estate-exposure-scan.py — weekly estate exposure enforcement (A341).

Detect -> auto-fix within GH_PAT scope -> record receipts -> escalate only what
cannot be fixed. The failing run IS the alert (no Discord layer, by design).

Modes:
  --selftest   Offline fixture harness (no network). Planted violations prove each
               detection fires; a planted-clean baseline proves zero false positives;
               the checked-in manifest is revalidated. Runs as the workflow's FIRST
               step, so a broken machinery can never mutate a live estate.
  --scan       Live: probe the 11 estate repos via GH_PAT, evaluate against the
               manifest baseline, auto-fix within token scope, write findings
               artifact + job summary, exit per the contract below.

Exit contract (house convention, org-malware-rescan.sh):
  0 = clean, or every finding was auto-fixed successfully
  1 = unfixable findings remain  (record-only classes + failed auto-fixes) — THE ALERT
  2 = infra failure (unreachable estate repo, manifest drift, unprobeable token) —
      fail-closed: a blind spot is never reported as healthy

Check classes (manifest drives repo set + expected visibility):
  code-visibility    code repo visibility != expected  -> RECORD/ESCALATE ONLY
                     (flipping visibility is an estate-ops decision, never automated)
  visibility waivers jobs-data-2026 is WAIVED to private during the visibility-flip
                     lock window (round-two F2): observed-private = no finding (shown
                     as WAIVED in coverage); observed-public while waived = LOW
                     waiver-stale finding so waiver rows can never become permanent
                     furniture; any third state falls through to the normal class.
  open-pr            open PR on a code repo            -> AUTO-CLOSE (silent: no comment,
                     no Discord; the closure event itself notifies the PR author)
  ruleset-missing    guard-required-main ruleset absent-> AUTO-ADD (template below)
  ruleset exemption  jobs-data-2026 is EXEMPT from the guard ruleset (round-two F1):
                     ungated wave pushes ~every 10 min — a required-check ruleset
                     there would reject every wave push and break production. Its
                     designated state is NO guard ruleset; the auto-add refuses this
                     repo (defense in depth), and a guard ruleset APPEARING there is
                     a HIGH ruleset-exemption-violated finding (production-breaking,
                     escalate immediately).
  ruleset-drift      guard ruleset present but off-template (inactive, no guard
                     context, not main, bypass actors, strict=true) -> RECORD ONLY
  ruleset-duplicate  >1 guard ruleset on main          -> RECORD ONLY
  describing-readme  README (any common variant: README.md/readme.md/README.rst/
                     readme.rst/README/README.txt) on a public code repo containing
                     the brand token ("zapply", case-insensitive) -> AUTO-STUB (one-
                     line neutral replacement; the FULL original text is preserved
                     in the receipt. NOTE: the old blob remains reachable in git
                     history until a history rewrite, which is an estate-ops
                     decision — recorded in the receipt, not hidden)
  content-visibility content repo visibility drift     -> RECORD ONLY (by design)
  token-expiry       GH_PAT expires in <14d (header probe) -> RECORD ONLY
                     (round-two F4: the estate PAT is fine-grained per the secrets
                     inventory, so the github-authentication-token-expiration header
                     is always present; a CLASSIC token would trip the fail-closed
                     INFRA path BY DESIGN — no fallback, a token whose expiry cannot
                     be observed is a coverage hole)

Anti-false-positive pins:
  - The ruleset predicate accepts every known main-ref form (~DEFAULT_BRANCH, ~MAIN,
    refs/heads/main) and tolerates unknown extra fields on an otherwise-canonical
    ruleset — a template extension upstream must not storm the run with drift.
  - AUTO-ADD fires only when NO ruleset carries a guard required_status_checks rule
    targeting main (in ANY enforcement state) — the machinery never creates the
    duplicate it would itself flag.
  - README scan is code repos only; content repos carry product branding by design.
  - Manifest is the designated-state baseline; the script cross-checks it against the
    canonical 11-repo estate every run (tampered/incomplete manifest = infra, exit 2).

Org-plane boundary: the scanner NEVER enumerates the org (no gh repo list). It probes
exactly the named manifest repos — inside the GH_PAT selection, no org-plane calls.

Coverage model (house rule): every manifest repo is probed or the run is INFRA.
  --dry-run   with --scan: detect + plan only; no mutating API call is made
              (findings stay unresolved -> exit 1 when violations exist, by design:
              a dress rehearsal that found violations shows red).

No new secrets: uses only the existing GH_PAT (workflow passes it as GH_TOKEN).
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ORG = "zapplyjobs"
CODE_REPOS = [
    "jobs-aggregator-engine",
    "job-board-aggregator",
    "job-board-consumer",
    "job-board-processing",
    "job-board-shared",
    "jobs-data-2026",
]
CONTENT_REPOS = [
    "resume-samples-2026",
    "interview-handbook-2026",
    "release-scheduling-bot",
    "underclassmen-internships",
    "Research-Internships-for-Undergraduates",
]
ESTATE_REPOS = CODE_REPOS + CONTENT_REPOS

# The brand token: any occurrence (case-insensitive) in a public code repo README is
# describing-the-operation. Boundary: this targets accidental description, not
# adversarial obfuscation (adversarial shapes are org-malware-rescan's IOC domain).
DESCRIBING_README_RE = re.compile(r"zapply", re.IGNORECASE)
NEUTRAL_STUB = "Public job board repository.\n"
TOKEN_EXPIRY_WARN_DAYS = 14.0
# README variants probed in order (round-two F3: hardcoding README.md left
# readme.md/README.rst/... silently unprobed). The stub rewrites the MATCHED path.
README_CANDIDATES = ("README.md", "readme.md", "README.rst", "readme.rst", "README", "README.txt")
README_SIZE_LIMIT = 1_000_000  # contents API stops returning content past 1MB

MAIN_REF_FORMS = {"refs/heads/main", "~DEFAULT_BRANCH", "~MAIN"}

# Canonical guard-required-main ruleset POST body (pinned to shared template id
# 21692193 per operator brief; the predicate above tolerates live variants).
RULESET_NAME = "guard-required-main"
RULESET_BODY = {
    "name": RULESET_NAME,
    "target": "branch",
    "enforcement": "active",
    "bypass_actors": [],
    "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
    "rules": [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": "guard"}],
            },
        }
    ],
}

SEVERITY = {
    "code-visibility": "high",
    "describing-readme": "high",
    "ruleset-missing": "high",
    "ruleset-exemption-violated": "high",
    "ruleset-drift": "medium",
    "ruleset-duplicate": "medium",
    "open-pr": "medium",
    "content-visibility": "medium",
    "token-expiry": "medium",
    "waiver-stale": "low",
}

# Lives beside the scanner inside scripts/** — the repo content gate (INF-REPO-C
# ONTENT-GATE-1, allowlist scripts/repo_content_gate.json in zapplyjobs/.github)
# is deny-by-default for tracked paths on gated repos; scripts/** is allowlisted,
# a root-level manifest is not. Same-dir derivation also removes checkout-root
# coupling.
MANIFEST_PATH = Path(__file__).resolve().parent / "estate-visibility-manifest.txt"


# --------------------------------------------------------------------------
# Pure core: evaluate_state + plan_actions (network-free; the selftest surface)
# --------------------------------------------------------------------------

def ruleset_guard_status(ruleset):
    """Classify a branch ruleset: 'canonical' | 'drifted' | 'unrelated'.

    unrelated = does not carry a guard required_status_checks rule targeting main
    (other rulesets on the repo are not this machinery's concern).
    drifted   = carries the guard rule targeting main but is off-template.
    canonical = the pinned shape (guard context, active, main targeted, no bypass,
    strict=false); unknown extra fields are tolerated.
    """
    if ruleset.get("target") != "branch":
        return "unrelated"
    conds = ruleset.get("conditions") or {}
    ref = conds.get("ref_name") or {}
    inc = ref.get("include") or []
    exc = ref.get("exclude") or []
    targets_main = any(r in MAIN_REF_FORMS for r in inc) and "refs/heads/main" not in exc
    guard_rule = None
    for rule in ruleset.get("rules") or []:
        if rule.get("type") == "required_status_checks":
            guard_rule = rule
            break
    if not guard_rule or not targets_main:
        return "unrelated"
    params = guard_rule.get("parameters") or {}
    contexts = [c.get("context") for c in (params.get("required_status_checks") or [])]
    essentials_ok = (
        "guard" in contexts
        and ruleset.get("enforcement") == "active"
        and params.get("strict_required_status_checks_policy") is False
        and not (ruleset.get("bypass_actors") or [])
    )
    return "canonical" if essentials_ok else "drifted"


def evaluate_state(state, manifest, now):
    """Pure: normalized live state + manifest baseline -> findings list.

    Findings are dicts: {id, class, repo, severity, fixable, action, detail}.
    Deterministic: sorted by (repo, class, id); no timestamps inside ids.
    """
    findings = []

    # (f) token expiry — normalized (days_remaining) by collect_live_state, which
    # owns the header parsing and the fail-closed infra path for unparseable values
    tok = state.get("token_expiry") or {}
    days = tok.get("days_remaining")
    if days is not None and days < TOKEN_EXPIRY_WARN_DAYS:
        expires = tok.get("expires", "?")
        findings.append({
            "id": "token-expiry:gh-pat",
            "class": "token-expiry",
            "repo": "-",
            "severity": SEVERITY["token-expiry"],
            "fixable": False,
            "action": "record-only",
            "detail": f"GH_PAT expires {expires} ({days}d < {TOKEN_EXPIRY_WARN_DAYS:.0f}d) — renew at github.com/settings/personal-access-tokens; header-probe receipt in findings artifact",
            "days_remaining": days,
        })

    repos = state.get("repos") or {}
    for repo, estate in _manifest_repos(manifest).items():
        rs = repos.get(repo) or {}
        cls = "code" if estate["class"] == "code" else "content"

        if not rs.get("reachable"):
            # collect_live_state already escalated to infra; nothing here (fail-closed
            # at the exit-code layer, not as a finding class).
            continue

        # (a)/(e) visibility vs designated baseline, honoring waivers (round-two F2).
        # A waiver ONLY waives the visibility finding — it must never suppress the
        # repo's other probes (F9/F10 exist precisely to pin that).
        expected = estate["expected_visibility"]
        actual = rs.get("visibility")
        waiver = (manifest.get("visibility_waivers") or {}).get(repo)
        waived_ok = bool(waiver) and actual == waiver.get("expected_during_waiver")
        if not waived_ok:
            if waiver and actual == expected:
                findings.append({
                    "id": f"waiver-stale:{repo}",
                    "class": "waiver-stale",
                    "repo": repo,
                    "severity": SEVERITY["waiver-stale"],
                    "fixable": False,
                    "action": "record-only",
                    "detail": f"visibility waiver no longer matches live state (actual={actual}, baseline={expected}) — remove the waiver row to keep the manifest honest; waivers must never become permanent furniture",
                })
            elif actual != expected:
                if cls == "code":
                    findings.append({
                        "id": f"code-visibility:{repo}",
                        "class": "code-visibility",
                        "repo": repo,
                        "severity": SEVERITY["code-visibility"],
                        "fixable": False,
                        "action": "record-only",
                        "detail": f"visibility={actual}, expected={expected} — flipping visibility is an estate-ops decision, never automated",
                    })
                else:
                    findings.append({
                        "id": f"content-visibility:{repo}",
                        "class": "content-visibility",
                        "repo": repo,
                        "severity": SEVERITY["content-visibility"],
                        "fixable": False,
                        "action": "record-only",
                        "detail": f"visibility={actual}, expected={expected} — content repos are record-only by design",
                    })

        if cls != "code" or rs.get("archived"):
            continue  # content repos: visibility only; archived repos: no mutation probes

        # (b) open PRs -> auto-close. Machinery-authored stub PRs (head estate-exposure/*)
        # are exempt — the machinery must never close its own fix PRs (live-learned:
        # run 1's stub PRs would otherwise be auto-closed on the next run).
        for pr in rs.get("open_prs") or []:
            if (pr.get("head_ref") or "").startswith("estate-exposure/"):
                continue
            findings.append({
                "id": f"open-pr:{repo}:{pr['number']}",
                "class": "open-pr",
                "repo": repo,
                "severity": SEVERITY["open-pr"],
                "fixable": True,
                "action": "auto-close",
                "detail": f"open PR #{pr['number']} '{pr.get('title', '')}' by {pr.get('author', '?')} — auto-close silently",
                "pr_number": pr["number"],
            })

        # (c) guard ruleset — with data-2026 exemption (round-two F1: ungated wave
        # pushes; a required-check ruleset there breaks production on arrival)
        guard_rulesets = [r for r in (rs.get("rulesets") or []) if ruleset_guard_status(r) in ("canonical", "drifted")]
        exempt = (manifest.get("ruleset_exemptions") or {}).get(repo)
        if exempt:
            if guard_rulesets:
                names = "; ".join(f"{r.get('name')}#{r.get('id')}({r.get('enforcement')})" for r in guard_rulesets)
                findings.append({
                    "id": f"ruleset-exemption-violated:{repo}",
                    "class": "ruleset-exemption-violated",
                    "repo": repo,
                    "severity": SEVERITY["ruleset-exemption-violated"],
                    "fixable": False,
                    "action": "record-only",
                    "detail": f"PRODUCTION RISK: guard ruleset present on exempt repo ({exempt.get('until', 'no until recorded')}): {names} — required checks reject this repo's ungated wave pushes; remove via estate-ops immediately",
                })
            # designated state = no guard ruleset: missing/drift/duplicate checks skipped
        elif not guard_rulesets:
            findings.append({
                "id": f"ruleset-missing:{repo}",
                "class": "ruleset-missing",
                "repo": repo,
                "severity": SEVERITY["ruleset-missing"],
                "fixable": True,
                "action": "auto-add",
                "detail": f"no guard-required-main ruleset — auto-add from template (shared id 21692193 shape)",
            })
        else:
            canonical = [r for r in guard_rulesets if ruleset_guard_status(r) == "canonical"]
            if not canonical:
                names = "; ".join(f"{r.get('name')}#{r.get('id')}({r.get('enforcement')})" for r in guard_rulesets)
                findings.append({
                    "id": f"ruleset-drift:{repo}",
                    "class": "ruleset-drift",
                    "repo": repo,
                    "severity": SEVERITY["ruleset-drift"],
                    "fixable": False,
                    "action": "record-only",
                    "detail": f"guard ruleset present but off-template: {names} — shared-template sync is an estate-ops decision",
                })
            elif len(canonical) > 1:
                names = "; ".join(f"{r.get('name')}#{r.get('id')}" for r in canonical)
                findings.append({
                    "id": f"ruleset-duplicate:{repo}",
                    "class": "ruleset-duplicate",
                    "repo": repo,
                    "severity": SEVERITY["ruleset-duplicate"],
                    "fixable": False,
                    "action": "record-only",
                    "detail": f"{len(canonical)} canonical guard rulesets on main: {names} — dedupe is an estate-ops decision",
                })

        # (d) describing README on a public code repo
        readme = rs.get("readme") or {}
        if readme.get("present") and isinstance(readme.get("text"), str):
            if DESCRIBING_README_RE.search(readme["text"]):
                findings.append({
                    "id": f"describing-readme:{repo}",
                    "class": "describing-readme",
                    "repo": repo,
                    "severity": SEVERITY["describing-readme"],
                    "fixable": True,
                    "action": "auto-stub",
                    "readme_path": readme.get("path") or "README.md",
                    "detail": "README describes the operation (brand token match) — auto-stub to one-line neutral; original preserved in receipt; old blob stays in git history until estate-ops history rewrite",
                })

    findings.sort(key=lambda f: (f["repo"], f["class"], f["id"]))
    return findings


def plan_actions(findings, manifest=None):
    """Pure: findings -> mutation plan (only fixable classes; payload per class)."""
    exempt_repos = set((manifest or {}).get("ruleset_exemptions") or {})
    actions = []
    for f in findings:
        if f["class"] == "open-pr":
            actions.append({
                "finding": f["id"], "type": "close-pr", "repo": f["repo"],
                "payload": {"pr_number": f["pr_number"]},
            })
        elif f["class"] == "ruleset-missing":
            if f["repo"] in exempt_repos:
                # defense in depth: the evaluator suppresses ruleset-missing on exempt
                # repos, so reaching this line means manifest drifted mid-run — refuse
                # the loaded gun (round-two F1: a required-check ruleset on data-2026
                # rejects every ungated wave push and breaks production)
                continue
            actions.append({
                "finding": f["id"], "type": "add-ruleset", "repo": f["repo"],
                "payload": dict(RULESET_BODY),
            })
        elif f["class"] == "describing-readme":
            actions.append({
                "finding": f["id"], "type": "stub-readme", "repo": f["repo"],
                "payload": {
                    "path": f.get("readme_path", "README.md"),
                    "content_b64": base64.b64encode(NEUTRAL_STUB.encode()).decode(),
                    "message": "estate-exposure: stub describing README (auto-fix, receipt keeps original)",
                },
            })
    return actions


def _manifest_repos(manifest):
    """Validated {repo: {class, expected_visibility, record_only}} from the manifest."""
    estate = manifest.get("estate") or {}
    code = estate.get("code_repos") or []
    content = estate.get("content_repos") or []
    expected = manifest.get("expected_visibility") or {}
    record_only = manifest.get("record_only") or {}
    repos = {}
    for name in code:
        repos[name] = {"class": "code", "expected_visibility": expected.get(name),
                       "record_only": bool(record_only.get(name))}
    for name in content:
        repos[name] = {"class": "content", "expected_visibility": expected.get(name),
                       "record_only": bool(record_only.get(name))}
    return repos


def validate_manifest(manifest):
    """Return list of manifest problems ([] = healthy). Manifest drift = infra."""
    problems = []
    estate = manifest.get("estate") or {}
    code = estate.get("code_repos") or []
    content = estate.get("content_repos") or []
    expected = manifest.get("expected_visibility") or {}
    record_only = manifest.get("record_only") or {}
    if estate.get("org") != ORG:
        problems.append(f"estate.org={estate.get('org')!r}, expected {ORG!r}")
    if sorted(code) != sorted(CODE_REPOS):
        problems.append(f"code_repos mismatch: manifest={sorted(code)} canonical={sorted(CODE_REPOS)}")
    if sorted(content) != sorted(CONTENT_REPOS):
        problems.append(f"content_repos mismatch: manifest={sorted(content)} canonical={sorted(CONTENT_REPOS)}")
    if set(code) & set(content):
        problems.append("code/content overlap")
    for name in ESTATE_REPOS:
        if expected.get(name) not in ("public", "private"):
            problems.append(f"expected_visibility[{name}]={expected.get(name)!r} invalid")
    for name in content:
        if not record_only.get(name):
            problems.append(f"content repo {name} must be record_only=true")
    for name in code:
        if record_only.get(name):
            problems.append(f"code repo {name} must NOT be record_only")
    extra = (set(expected) | set(record_only)) - set(ESTATE_REPOS)
    if extra:
        problems.append(f"unknown repos in manifest: {sorted(extra)}")
    waivers = manifest.get("visibility_waivers") or {}
    for name, w in waivers.items():
        if name not in ESTATE_REPOS:
            problems.append(f"visibility_waivers[{name}]: unknown repo")
            continue
        if w.get("expected_during_waiver") not in ("public", "private"):
            problems.append(f"visibility_waivers[{name}].expected_during_waiver={w.get('expected_during_waiver')!r} invalid")
        elif w.get("expected_during_waiver") == expected.get(name):
            problems.append(f"visibility_waivers[{name}] is redundant (equals the baseline expectation) — a waiver must differ or it is furniture")
    exemptions = manifest.get("ruleset_exemptions") or {}
    for name in exemptions:
        if name not in CODE_REPOS:
            problems.append(f"ruleset_exemptions[{name}]: only code repos carry guard rulesets")
    return problems


# --------------------------------------------------------------------------
# Live adapter: collect_live_state (the only network path)
# --------------------------------------------------------------------------

class GhError(Exception):
    pass


def gh(args, retries=2):
    """gh api wrapper with retry-on-transient; returns (stdout, stderr). Raises GhError on failure."""
    env = dict(os.environ)
    last = None
    for attempt in range(retries + 1):
        proc = subprocess.run(["gh"] + args, capture_output=True, text=True, env=env, timeout=60)
        if proc.returncode == 0:
            return proc.stdout, proc.stderr
        last = proc
        transient = any(s in (proc.stderr or "") for s in ("502", "503", "504", "timed out", "connection reset", "rate limit"))
        if not transient or attempt == retries:
            break
        time.sleep(2 * (attempt + 1))
    raise GhError((last.stderr or last.stdout or "gh failed").strip()[:400])


def _gh_json(args, **kw):
    out, _ = gh(args, **kw)
    return json.loads(out)


def _parse_expiry(raw):
    """Parse a GitHub token-expiration header value -> aware UTC datetime (None if
    unparseable). Handles both observed shapes: '2026-11-30T17:40:22Z' and the live
    '2026-11-30 17:40:22 UTC'. GitHub expirations are always UTC."""
    s = (raw or "").strip()
    s = re.sub(r"\s+UTC$", "+00:00", s, flags=re.IGNORECASE)
    s = re.sub(r"(\d) (\d{2}:\d{2})", r"\1T\2", s, count=1)  # space date-time separator
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def collect_live_state():
    """Probe the estate via GH_PAT (from GH_TOKEN env). Returns (state, infra[]).

    Fail-closed coverage: any unreachable estate repo lands in infra (exit 2),
    never silently skipped.
    """
    infra = []
    state = {"token_expiry": None, "repos": {}}

    for repo in ESTATE_REPOS:
        rs = {"reachable": False, "archived": False, "visibility": None,
              "default_branch": None, "open_prs": None, "rulesets": None, "readme": None}
        try:
            # -i: headers ride stdout ahead of the body; the fine-grained PAT puts its
            # expiry in github-authentication-token-expiration on every response.
            # REAL header shape (live-verified 2026-11-15, run 43658142137):
            #   "Github-Authentication-Token-Expiration: 2026-11-30 17:40:22 UTC"
            # — spaced, UTC suffix, title-cased name. The \S+ variant captured only
            # the date and crashed the aware/naive subtraction on run 1.
            out_i, _ = gh(["api", f"repos/{ORG}/{repo}", "-i"])
            m = re.search(r"(?im)^github-authentication-token-expiration:\s*(.+)$", out_i)
            if m and state["token_expiry"] is None:
                exp_dt = _parse_expiry(m.group(1))
                if exp_dt is None:
                    infra.append(f"GH_PAT expiry header unparseable (fail-closed): {m.group(1)!r}")
                else:
                    state["token_expiry"] = {
                        "expires": exp_dt.isoformat().replace("+00:00", "Z"),
                        "days_remaining": round((exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0, 2),
                        "probed_on": repo,
                    }
            meta = _gh_json(["api", f"repos/{ORG}/{repo}"])
            rs.update(reachable=True, visibility=meta.get("visibility"),
                      archived=bool(meta.get("archived")),
                      default_branch=meta.get("default_branch") or "main")
            if not rs["archived"]:
                if repo in CODE_REPOS:
                    rs["open_prs"] = [
                        {"number": p["number"], "title": (p.get("title") or "")[:120],
                         "author": ((p.get("user") or {}).get("login") or "?"),
                         "head_ref": ((p.get("head") or {}).get("ref") or "")}
                        for p in _paginate(repo, "open PRs", f"repos/{ORG}/{repo}/pulls?state=open", infra)
                    ]
                    # rulesets list then detail-fetch candidates (list omits rules)
                    summaries = _paginate(repo, "rulesets", f"repos/{ORG}/{repo}/rulesets", infra)
                    details = []
                    for s in summaries:
                        if s.get("target") and s.get("target") != "branch":
                            continue
                        try:
                            d = _gh_json(["api", f"repos/{ORG}/{repo}/rulesets/{s['id']}"])
                        except GhError as e:
                            infra.append(f"{repo}: ruleset {s.get('id')} detail fetch failed (fail-closed): {e}")
                            continue
                        details.append(d)
                    rs["rulesets"] = details
                    readme = _fetch_readme(repo, rs["default_branch"], infra)
                    rs["readme"] = readme
        except GhError as e:
            infra.append(f"{repo}: probe failed (fail-closed): {e}")
        state["repos"][repo] = rs

    if state["token_expiry"] is None:
        infra.append("GH_PAT expiry header never observed across estate probes — expiry uncheckable (fail-closed)")
    return state, infra


def _fetch_readme(repo, default_branch, infra):
    """README probe across common variants (round-two F3: hardcoding README.md left
    readme.md/README.rst/... silently unprobed). Returns {present, path, sha, text}.
    Empty file (content=="" with size 0) is benign -> text "" (round-two missed-case f:
    it used to be misfiled as >1MB -> INFRA on a benign state). >1MB stays fail-closed.
    """
    for path in README_CANDIDATES:
        try:
            out, _ = gh(["api", f"repos/{ORG}/{repo}/contents/{path}?ref={default_branch}"])
            meta = json.loads(out)
            content = meta.get("content") or ""
            size = meta.get("size") or 0
            sha = meta.get("sha")
            if not content:
                if size > README_SIZE_LIMIT:
                    infra.append(f"{repo}: {path} >1MB via contents API — unprobeable (fail-closed)")
                    return {"present": True, "path": path, "text": None, "sha": sha}
                return {"present": True, "path": path, "text": "", "sha": sha}  # empty file: benign
            text = base64.b64decode(content).decode("utf-8", "replace")
            return {"present": True, "path": path, "text": text, "sha": sha}
        except GhError as e:
            if "404" in str(e) or "Not Found" in str(e):
                continue  # try the next variant
            infra.append(f"{repo}: README probe failed ({path}) (fail-closed): {e}")
            return {"present": True, "path": path, "text": None, "sha": None}
    return {"present": False, "path": None, "text": None, "sha": None}


def _paginate(repo, what, path, infra, cap=10, per_page=100):
    """Page through a list endpoint; truncation at cap pages is fail-closed (round-two
    F5: silent truncation read PR #101 as green and hid page-2 rulesets as missing)."""
    sep = "&" if "?" in path else "?"
    items = []
    for page in range(1, cap + 1):
        batch = _gh_json(["api", f"{path}{sep}per_page={per_page}&page={page}"])
        items.extend(batch)
        if len(batch) < per_page:
            return items
    infra.append(f"{repo}: {what} truncated at {cap * per_page} items (fail-closed)")
    return items


# --------------------------------------------------------------------------
# Apply (mutating; dry_run validates payloads without calling the API)
# --------------------------------------------------------------------------

def apply_actions(actions, state, dry_run):
    receipts = []
    for a in actions:
        rec = {"finding": a["finding"], "action": a["type"], "repo": a["repo"], "result": None, "detail": ""}
        try:
            if dry_run:
                rec["result"] = "dry-run-validated"
                rec["detail"] = f"payload: {json.dumps(a['payload'], sort_keys=True)[:300]}"
            elif a["type"] == "close-pr":
                n = a["payload"]["pr_number"]
                gh(["api", f"repos/{ORG}/{a['repo']}/pulls/{n}", "-X", "PATCH",
                    "-f", "state=closed", "--jq", ".state"])
                rec["result"] = "applied"
                rec["detail"] = f"PR #{n} closed (no comment — silent per design)"
            elif a["type"] == "add-ruleset":
                body = json.dumps(a["payload"])
                proc = subprocess.run(
                    ["gh", "api", f"repos/{ORG}/{a['repo']}/rulesets", "-X", "POST",
                     "--input", "-", "--jq", ".id"],
                    input=body, capture_output=True, text=True,
                    env={**os.environ, "GH_TOKEN": os.environ.get("GH_TOKEN", "")}, timeout=60)
                if proc.returncode != 0:
                    raise GhError(proc.stderr.strip()[:400])
                rec["result"] = "applied"
                rec["detail"] = f"ruleset created id={proc.stdout.strip()} (guard-required-main template)"
            elif a["type"] == "stub-readme":
                sha = ((state.get("repos", {}).get(a["repo"]) or {}).get("readme") or {}).get("sha")
                if not sha:
                    raise GhError("README sha unknown — cannot PUT contents (state stale)")
                body = json.dumps({"message": a["payload"]["message"],
                                   "content": a["payload"]["content_b64"], "sha": sha})
                path_q = a["payload"]["path"]
                rec["original_readme"] = ((state.get("repos", {}).get(a["repo"]) or {}).get("readme") or {}).get("text") or ""
                direct = _gh_proc(["api", f"repos/{ORG}/{a['repo']}/contents/{path_q}", "-X", "PUT",
                                   "--input", "-", "--jq", ".commit.sha"], body)
                if direct.returncode == 0:
                    rec["result"] = "applied"
                    rec["detail"] = f"README stubbed direct-to-main (commit {direct.stdout.strip()[:12]}); original preserved below"
                else:
                    # Gated repo (live-learned, run 43658448660): the guard ruleset rejects
                    # direct-to-main writes (HTTP 409 'required status check guard').
                    # Route through the PR flow: branch -> commit -> PR -> wait for the
                    # guard check on the PR head -> merge. The check runs because
                    # repo-guard.yml triggers on pull_request to main.
                    if "409" not in (direct.stderr or "") and "rule" not in (direct.stderr or "").lower():
                        raise GhError(direct.stderr.strip()[:400])
                    rec["result"] = "applied"
                    rec["detail"] = _stub_via_pr(a, state, rec)
            else:
                raise GhError(f"unknown action type {a['type']}")
        except GhError as e:
            rec["result"] = "failed"
            rec["detail"] = f"auto-fix failed (escalated): {e}"
        receipts.append(rec)
    return receipts


def _gh_proc(args, stdin_body):
    return subprocess.run(["gh"] + args, input=stdin_body, capture_output=True, text=True,
                          env={**os.environ, "GH_TOKEN": os.environ.get("GH_TOKEN", "")}, timeout=120)


STUB_BRANCH = "estate-exposure/stub-readme"


def _stub_via_pr(action, state, receipt):
    """Branch + commit + PR + guard-check wait + merge. Returns receipt detail.
    Raises GhError (receipt escalates) when the PR flow cannot complete."""
    repo = action["repo"]
    path_q = action["payload"]["path"]
    branch = STUB_BRANCH

    # reuse an already-open machinery PR if one exists (re-run while a PR is pending)
    existing = [p for p in (state.get("repos", {}).get(repo, {}).get("open_prs") or [])
                if (p.get("head_ref") or "") == branch]
    pr_number = existing[0]["number"] if existing else None

    if not pr_number:
        base_sha = _gh_json(["api", f"repos/{ORG}/{repo}/git/ref/heads/{state['repos'][repo]['default_branch']}"])["object"]["sha"]
        proc = _gh_proc(["api", f"repos/{ORG}/{repo}/git/refs", "-X", "POST", "--input", "-"],
                        json.dumps({"ref": f"refs/heads/{branch}", "sha": base_sha}))
        if proc.returncode != 0 and "422" not in (proc.stderr or "") and "already exists" not in (proc.stderr or ""):
            raise GhError(f"branch create failed: {proc.stderr.strip()[:300]}")
        # PUT onto the branch (sha is the file state on that branch; refetch)
        meta = _gh_json(["api", f"repos/{ORG}/{repo}/contents/{path_q}?ref={branch}"])
        body = json.dumps({"message": action["payload"]["message"],
                           "content": action["payload"]["content_b64"],
                           "sha": meta.get("sha"), "branch": branch})
        proc = _gh_proc(["api", f"repos/{ORG}/{repo}/contents/{path_q}", "-X", "PUT",
                         "--input", "-", "--jq", ".commit.sha"], body)
        if proc.returncode != 0:
            raise GhError(f"branch commit failed: {proc.stderr.strip()[:300]}")
        head_sha = proc.stdout.strip()
        pr_body = (f"estate-exposure auto-fix: stubs a describing README (brand-token match) with a "
                   f"one-line neutral replacement.\n\nOriginal text preserved for the record:\n\n```\n"
                   f"{(receipt.get('original_readme') or '')[:800]}\n```\n\n"
                   f"Opened by the estate-exposure workflow; merged automatically once the guard check passes.")
        proc = _gh_proc(["api", f"repos/{ORG}/{repo}/pulls", "-X", "POST", "--input", "-", "--jq", ".number"],
                        json.dumps({"title": "estate-exposure: stub describing README",
                                    "head": branch, "base": state["repos"][repo]["default_branch"],
                                    "body": pr_body}))
        if proc.returncode != 0:
            raise GhError(f"PR create failed: {proc.stderr.strip()[:300]}")
        pr_number = int(proc.stdout.strip())
    else:
        head_sha = _gh_json(["api", f"repos/{ORG}/{repo}/pulls/{pr_number}"])["head"]["sha"]

    # wait for the required "guard" check on the PR head (repo-guard.yml on pull_request)
    deadline = time.time() + 300
    guard = None
    while time.time() < deadline:
        time.sleep(15)
        runs = _gh_json(["api", f"repos/{ORG}/{repo}/commits/{head_sha}/check-runs?check_name=guard"])
        items = runs.get("check_runs") or []
        if items and items[0].get("status") == "completed":
            guard = items[0].get("conclusion")
            break
    if guard != "success":
        raise GhError(f"guard check on stub PR #{pr_number} did not pass within 5m (conclusion={guard}) — escalated")

    proc = _gh_proc(["api", f"repos/{ORG}/{repo}/pulls/{pr_number}/merge", "-X", "PUT", "--input", "-",
                     "--jq", ".merged"], json.dumps({"merge_method": "squash"}))
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise GhError(f"stub PR #{pr_number} merge failed: {proc.stderr.strip()[:300]}")
    return f"README stubbed via PR #{pr_number} (branch {branch}, guard check passed, squash-merged); original preserved below"


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def verdict_of(findings, receipts, infra):
    if infra:
        return "INFRA"
    failed = [r for r in receipts if r["result"] == "failed"]
    unresolved = [f for f in findings if not _resolved(f, receipts)]
    if unresolved or failed:
        return "ALERT"
    if findings:
        return "ALL-FIXED"
    return "CLEAN"


def _resolved(finding, receipts):
    return any(r["finding"] == finding["id"] and r["result"] == "applied" for r in receipts)


def render_summary(mode, verdict, findings, receipts, infra, state, manifest, now):
    lines = [f"# Estate exposure — {mode} — {now.strftime('%Y-%m-%dT%H:%MZ')}", ""]
    lines.append(f"**Verdict: {verdict}** — "
                 + ("scan incomplete (fail-closed): fix coverage before trusting any green" if verdict == "INFRA"
                    else "unfixable findings remain; this failing run IS the alert" if verdict == "ALERT"
                    else "every finding was auto-fixed; receipts below" if verdict == "ALL-FIXED"
                    else "estate matches the designated-state baseline"))
    lines.append("")
    lines.append(f"## Findings ({len(findings)})")
    lines.append("")
    if findings:
        lines.append("| Class | Repo | Sev | Disposition | Detail |")
        lines.append("|---|---|---|---|---|")
        for f in findings:
            disp = _disposition(f, receipts)
            lines.append(f"| {f['class']} | {f['repo']} | {f['severity']} | {disp} | {f['detail']} |")
    else:
        lines.append("None.")
    lines.append("")
    lines.append(f"## Coverage ({len(ESTATE_REPOS)} estate repos)")
    lines.append("")
    lines.append("| Probe | Result |")
    lines.append("|---|---|")
    tok = state.get("token_expiry") or {}
    lines.append(f"| GH_PAT expiry | {tok.get('expires', 'NOT OBSERVED (infra)')}"
                 + (f" ({tok.get('days_remaining', '?')}d remaining)" if tok.get("days_remaining") is not None else "") + " |")
    repos = state.get("repos") or {}
    waivers = manifest.get("visibility_waivers") or {}
    exemptions = manifest.get("ruleset_exemptions") or {}
    for repo in ESTATE_REPOS:
        rs = repos.get(repo) or {}
        vis = rs.get("visibility") or "UNREACHABLE"
        w = waivers.get(repo)
        if w and vis == w.get("expected_during_waiver"):
            vis += f" WAIVED (until: {w.get('until', 'unrecorded')})"
        probes = []
        if rs.get("reachable"):
            if repo in CODE_REPOS and not rs.get("archived"):
                if repo in exemptions:
                    probes.append("rulesets=EXEMPT (guard ruleset must NOT exist here)")
                else:
                    probes.append(f"rulesets={len(rs.get('rulesets') or [])}")
                probes.append(f"prs={len(rs.get('open_prs') or [])}")
                rd = rs.get("readme") or {}
                probes.append(f"readme={rd.get('path') or 'absent'}")
        lines.append(f"| {repo} | vis={vis} {' '.join(probes)} |")
    if infra:
        lines.append("")
        lines.append("## Infra (fail-closed — run verdict is INFRA)")
        lines.append("")
        for i in infra:
            lines.append(f"- {i}")
    if receipts:
        lines.append("")
        lines.append("## Auto-fix receipts")
        lines.append("")
        lines.append("| Finding | Action | Result | Detail |")
        lines.append("|---|---|---|---|")
        for r in receipts:
            d = r.get("detail", "")
            if r.get("original_readme"):
                d += f"<br>ORIGINAL: {r['original_readme'][:500]!r}"
            lines.append(f"| {r['finding']} | {r['action']} | {r['result']} | {d} |")
    return "\n".join(lines) + "\n"


def _disposition(finding, receipts):
    if not finding["fixable"]:
        return "record-only (escalate)"
    for r in receipts:
        if r["finding"] == finding["id"]:
            if r["result"] == "applied":
                return "auto-fixed"
            if r["result"] == "failed":
                return "AUTO-FIX FAILED"
            if r["result"] == "dry-run-validated":
                return "planned (dry-run)"
    return "planned"


def write_findings_artifact(path, mode, verdict, findings, receipts, infra, state):
    payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "verdict": verdict,
        "findings": findings,
        "receipts": receipts,
        "infra": infra,
        "coverage": {
            "estate_repos": len(ESTATE_REPOS),
            "reachable": sum(1 for r in (state.get("repos") or {}).values() if r.get("reachable")),
            "unreachable": [r for r, v in (state.get("repos") or {}).items() if not v.get("reachable")],
            "token_expiry": state.get("token_expiry"),
        },
    }
    Path(path).write_text(json.dumps(payload, indent=1))
    return payload


# --------------------------------------------------------------------------
# Fixtures (embedded — the brief bounds files to workflow+script+manifest)
# --------------------------------------------------------------------------

def _fixture_manifest():
    expected = {r: "public" for r in ESTATE_REPOS}
    record_only = {r: True for r in CONTENT_REPOS}
    return {"estate": {"org": ORG, "code_repos": list(CODE_REPOS), "content_repos": list(CONTENT_REPOS)},
            "expected_visibility": expected, "record_only": record_only,
            # mirrors the real manifest's round-two rows (F1/F2)
            "visibility_waivers": {"jobs-data-2026": {
                "expected_during_waiver": "private",
                "until": "visibility revert lands (lead unlock signal)",
                "reason": "temporarily private pending revert"}},
            "ruleset_exemptions": {"jobs-data-2026": {
                "until": "gated-push migration exists for wave-push producers",
                "reason": "ungated wave pushes ~every 10 min"}}}


def _transition_form(state):
    """Put jobs-data-2026 into its designated TRANSITION form: private (waived),
    no guard ruleset (exempt), stubbed neutral README. This is what 'clean' means
    for that repo until gated-push migration + visibility revert land."""
    state["jobs-data-2026"] = _probed_code_state(
        visibility="private", rulesets=[],
        readme={"present": True, "path": "README.md", "text": NEUTRAL_STUB, "sha": "stub"})
    return state


def _repo_state(visibility="public", open_prs=None, rulesets=None, readme=None, archived=False):
    return {"reachable": True, "visibility": visibility, "archived": archived,
            "default_branch": "main", "open_prs": open_prs, "rulesets": rulesets, "readme": readme}


def _probed_code_state(visibility="public", open_prs=None, rulesets=None, readme=None):
    """Fully-probed clean code repo (single-class fixtures still carry the other
    probes: None on live code repos means 'probed, found none' — never 'skipped')."""
    if open_prs is None:
        open_prs = []
    if rulesets is None:
        rulesets = [_canonical_ruleset()]
    if readme is None:
        readme = {"present": True, "text": "Neutral.\n", "sha": "x"}
    return _repo_state(visibility=visibility, open_prs=open_prs, rulesets=rulesets, readme=readme)


def _canonical_ruleset(**over):
    rs = {"id": 21692193, "name": RULESET_NAME, "target": "branch", "enforcement": "active",
          "bypass_actors": [], "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
          "rules": [{"type": "required_status_checks",
                     "parameters": {"strict_required_status_checks_policy": False,
                                    "required_status_checks": [{"context": "guard"}]}}]}
    rs.update(over)
    return rs


NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)


def build_fixtures():
    """Named fixture states. Deterministic; NOW pinned. Each = (state, extra, manifest, expected_ids).
    Every fixture ends in _transition_form: jobs-data-2026's designated state during the
    lock/migration window is private (waived) + no guard ruleset (exempt) + stub README.
    Fixtures that deliberately violate the transition form override AFTER it."""
    fm = _fixture_manifest()
    F = {}

    # F1 (a): non-exempt code repo private -> record-only (data-2026 is waived, so the
    # class fixture must live on another repo)
    state1 = {r: _probed_code_state() for r in CODE_REPOS}
    state1.update({r: _repo_state() for r in CONTENT_REPOS})
    state1["job-board-shared"] = _probed_code_state(visibility="private")
    F["F1-code-visibility"] = (_transition_form(state1),
                               {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                               {"code-visibility:job-board-shared"})

    # F2 (b): planted open PR -> auto-close plan
    state2 = {r: _probed_code_state() for r in CODE_REPOS}
    state2.update({r: _repo_state() for r in CONTENT_REPOS})
    state2["job-board-aggregator"] = _probed_code_state(
        open_prs=[{"number": 5, "title": "Bump actions/checkout from 5 to 6", "author": "dependabot[bot]"}])
    F["F2-open-pr"] = (_transition_form(state2), {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                       {"open-pr:job-board-aggregator:5"})

    # F3 (c): missing ruleset on a NON-exempt repo -> auto-add
    state3 = {r: _probed_code_state() for r in CODE_REPOS}
    state3.update({r: _repo_state() for r in CONTENT_REPOS})
    state3["job-board-consumer"] = _probed_code_state(rulesets=[])
    F["F3-ruleset-missing"] = (_transition_form(state3), {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                               {"ruleset-missing:job-board-consumer"})

    # F3b: guard ruleset present but enforcement=disabled -> drift (record-only, NO add)
    drifted = _canonical_ruleset(id=777, name="guard-required-main", enforcement="disabled")
    state3b = {r: _probed_code_state() for r in CODE_REPOS}
    state3b.update({r: _repo_state() for r in CONTENT_REPOS})
    state3b["job-board-shared"] = _probed_code_state(rulesets=[drifted])
    F["F3b-ruleset-drift"] = (_transition_form(state3b), {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                              {"ruleset-drift:job-board-shared"})

    # F3c: canonical + unknown extra fields -> NO finding (tolerant predicate; first-run FP guard)
    tolerant = _canonical_ruleset(id=999)
    tolerant["rules"][0]["parameters"]["extra_unknown"] = {"whatever": 1}
    tolerant["some_future_field"] = "ignored"
    state3c = {r: _probed_code_state() for r in CODE_REPOS}
    state3c.update({r: _repo_state() for r in CONTENT_REPOS})
    state3c["jobs-aggregator-engine"] = _probed_code_state(rulesets=[tolerant])
    F["F3c-ruleset-tolerant"] = (_transition_form(state3c), {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                                 set())

    # F4 (d): describing README (variant path readme.md to pin F3 fix) -> auto-stub
    state4 = {r: _probed_code_state() for r in CODE_REPOS}
    state4.update({r: _repo_state() for r in CONTENT_REPOS})
    state4["job-board-processing"] = _probed_code_state(
        readme={"present": True, "path": "readme.md", "sha": "deadbeef",
                "text": "Part of the [Zapply](https://zapply.jobs) job board infrastructure.\n"})
    F["F4-describing-readme"] = (_transition_form(state4), {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                                 {"describing-readme:job-board-processing"})

    # F5 (e): content repo private -> record-only; code side fully clean
    state5 = {r: _probed_code_state() for r in CODE_REPOS}
    state5.update({r: _repo_state() for r in CONTENT_REPOS})
    state5["resume-samples-2026"] = _repo_state(visibility="private")
    F["F5-content-visibility"] = (_transition_form(state5), {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                                  {"content-visibility:resume-samples-2026"})

    # F6 (f): token expiry boundary — 13.9d fires; 14.0d does not
    base = {r: _probed_code_state() for r in CODE_REPOS}
    base.update({r: _repo_state() for r in CONTENT_REPOS})
    F["F6a-token-expiry-fires"] = (_transition_form(dict(base)), {"token_expiry": {"expires": "2026-11-28T02:24:00Z", "days_remaining": 13.9}}, fm,
                                   {"token-expiry:gh-pat"})  # NOW + 13.9d
    F["F6b-token-expiry-boundary-clean"] = (_transition_form(dict(base)), {"token_expiry": {"expires": "2026-11-28T12:00:01Z", "days_remaining": 14.0}}, fm,
                                            set())  # NOW + 14.000d

    # F7: clean baseline in the TRANSITION designated state — zero findings, zero actions
    clean = {r: _probed_code_state(readme={"present": True, "path": "README.md",
                                           "text": "Docs for the public job listings pipeline.\n", "sha": "x"})
             for r in CODE_REPOS}
    clean.update({r: _repo_state() for r in CONTENT_REPOS})
    F["F7-clean-baseline"] = (_transition_form(clean), {"token_expiry": {"expires": "2026-12-14T12:00:00Z", "days_remaining": 30.0}}, fm, set())

    # F8: clean + fully absent readmes + content repos unprobed (None fields —
    # evaluator must not probe-fall-back)
    clean2 = {r: _probed_code_state(readme={"present": False, "path": None, "text": None, "sha": None})
              for r in CODE_REPOS}
    clean2.update({r: {"reachable": True, "visibility": "public", "archived": False,
                       "default_branch": "main", "open_prs": None, "rulesets": None, "readme": None}
                   for r in CONTENT_REPOS})
    F["F8-clean-no-readme"] = (_transition_form(clean2), {"token_expiry": {"expires": "2026-12-14T12:00:00Z", "days_remaining": 30.0}}, fm, set())

    # F9 (F1-fold): guard ruleset APPEARS on the exempt repo -> HIGH violation
    # (production-breaking; visibility is waived so this is the only finding)
    state9 = {r: _probed_code_state() for r in CODE_REPOS}
    state9.update({r: _repo_state() for r in CONTENT_REPOS})
    state9["jobs-data-2026"] = _probed_code_state(visibility="private", rulesets=[_canonical_ruleset()])
    F["F9-exemption-violated"] = (state9, {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                                  {"ruleset-exemption-violated:jobs-data-2026"})

    # F10 (F1-fold): drifted ruleset on the exempt repo -> still a violation (and NOT
    # ruleset-drift: the exemption suppresses the drift class, the presence is the crime)
    state10 = {r: _probed_code_state() for r in CODE_REPOS}
    state10.update({r: _repo_state() for r in CONTENT_REPOS})
    state10["jobs-data-2026"] = _probed_code_state(visibility="private", rulesets=[drifted])
    F["F10-exemption-violated-drifted"] = (state10, {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                                           {"ruleset-exemption-violated:jobs-data-2026"})

    # F11 (F2-fold): waived repo back to public (ruleset still absent = exempt state
    # holds) -> waiver-stale only (anti-furniture guard)
    state11 = {r: _probed_code_state() for r in CODE_REPOS}
    state11.update({r: _repo_state() for r in CONTENT_REPOS})
    state11["jobs-data-2026"] = _probed_code_state(visibility="public", rulesets=[])
    F["F11-waiver-stale"] = (state11, {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                             {"waiver-stale:jobs-data-2026"})

    # F12 (F3-fold + missed-case f): EMPTY readme (benign, was INFRA) + lowercase
    # readme.md present -> the variant probe reads it; no finding from the empty one
    state12 = {r: _probed_code_state() for r in CODE_REPOS}
    state12.update({r: _repo_state() for r in CONTENT_REPOS})
    state12["job-board-shared"] = _probed_code_state(
        readme={"present": True, "path": "README.md", "text": "", "sha": "e1"})
    state12["job-board-consumer"] = _probed_code_state(
        readme={"present": True, "path": "readme.md", "sha": "v1", "text": "Neutral.\n"})
    F["F12-empty-and-variant-readme"] = (_transition_form(state12), {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                                         set())

    # F13 (live-learned): the machinery's OWN stub PR (head estate-exposure/*) must be
    # exempt from auto-close — the machinery never closes its own fix PRs
    state13 = {r: _probed_code_state() for r in CODE_REPOS}
    state13.update({r: _repo_state() for r in CONTENT_REPOS})
    state13["job-board-aggregator"] = _probed_code_state(
        open_prs=[{"number": 9, "title": "estate-exposure: stub describing README",
                   "author": "z-apply", "head_ref": STUB_BRANCH}])
    F["F13-machinery-pr-exempt"] = (_transition_form(state13), {"token_expiry": {"expires": "2026-12-30T00:00:00Z", "days_remaining": 45.5}}, fm,
                                    set())

    return F


# --------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------

def selftest(log=print):
    failures = []

    def check(cid, cond, detail=""):
        if cond:
            log(f"SELFTEST PASS {cid}")
        else:
            log(f"SELFTEST FAIL {cid}: {detail}")
            failures.append(cid)

    fm = _fixture_manifest()

    # T0. fixture integrity: every non-archived code repo carries ALL probe fields
    # (None on live code repos means 'probed, found none' — a half-probed fixture
    # would skew single-class tests into spurious ruleset-missing findings)
    for name, (repos, extra, manifest, _) in build_fixtures().items():
        bad = [r for r in CODE_REPOS
               if not (repos.get(r) or {}).get("archived")
               and any((repos.get(r) or {}).get(k) is None for k in ("open_prs", "rulesets", "readme"))]
        check(f"T0-fixture-integrity-{name}", not bad, f"code repos with unprobed fields: {bad}")

    # 0. checked-in manifest (the real one CI ships) parses + matches canonical estate
    try:
        live_manifest = json.loads(MANIFEST_PATH.read_text())
        problems = validate_manifest(live_manifest)
        check("M1-manifest-valid", not problems, "; ".join(problems))
    except Exception as e:  # noqa: BLE001 — selftest must survive any file state
        check("M1-manifest-valid", False, f"manifest unreadable: {e}")

    # 1. fixture evaluations: exact expected finding sets
    for name, (repos, extra, manifest, expected_ids) in build_fixtures().items():
        state = {"repos": repos, **extra}
        findings = evaluate_state(state, manifest, NOW)
        got = {f["id"] for f in findings}
        check(f"T1-{name}", got == set(expected_ids),
              f"expected {sorted(expected_ids)}, got {sorted(got)}")

    # 2. clean baselines produce zero actions (no accidental mutation plan)
    for name in ("F7-clean-baseline", "F8-clean-no-readme", "F3c-ruleset-tolerant",
                 "F6b-token-expiry-boundary-clean", "F11-waiver-stale", "F12-empty-and-variant-readme"):
        repos, extra, manifest, expected_ids = build_fixtures()[name]
        state = {"repos": repos, **extra}
        actions = plan_actions(evaluate_state(state, manifest, NOW), manifest)
        check(f"T2-{name}-zero-actions", not actions, f"planned {actions}")

    # 3. action-plan payload pins
    f2 = build_fixtures()["F2-open-pr"]
    acts = plan_actions(evaluate_state({"repos": f2[0], **f2[1]}, f2[2], NOW), f2[2])
    check("T3-close-pr-payload", acts and acts[0]["type"] == "close-pr"
          and acts[0]["payload"] == {"pr_number": 5}, json.dumps(acts))

    f3 = build_fixtures()["F3-ruleset-missing"]
    acts = plan_actions(evaluate_state({"repos": f3[0], **f3[1]}, f3[2], NOW), f3[2])
    ok = (len(acts) == 1 and acts[0]["type"] == "add-ruleset"
          and acts[0]["repo"] == "job-board-consumer"
          and json.dumps(acts[0]["payload"], sort_keys=True) == json.dumps(RULESET_BODY, sort_keys=True))
    check("T3-add-ruleset-body-pinned-to-template", ok, json.dumps(acts))

    f4 = build_fixtures()["F4-describing-readme"]
    acts = plan_actions(evaluate_state({"repos": f4[0], **f4[1]}, f4[2], NOW), f4[2])
    stub = [a for a in acts if a["type"] == "stub-readme"]
    ok = (len(stub) == 1
          and base64.b64decode(stub[0]["payload"]["content_b64"]).decode() == NEUTRAL_STUB
          and stub[0]["payload"]["path"] == "readme.md")  # round-two F3: stub the MATCHED variant
    check("T3-stub-readme-payload", ok, json.dumps(acts))

    # 4. boundary: drifted ruleset must NOT plan an add (machinery never creates its own duplicate)
    f3b = build_fixtures()["F3b-ruleset-drift"]
    acts = plan_actions(evaluate_state({"repos": f3b[0], **f3b[1]}, f3b[2], NOW), f3b[2])
    check("T4-drift-plans-no-add", not acts, json.dumps(acts))

    # 4b. round-two F1 LOADED-GUN GUARD: plan_actions refuses auto-add on the exempt
    # repo even if a ruleset-missing finding somehow reaches the planner (manifest
    # drift mid-run class). Direct finding-level pin — the evaluator never emits it.
    finding = {"id": "ruleset-missing:jobs-data-2026", "class": "ruleset-missing",
               "repo": "jobs-data-2026", "severity": "high", "fixable": True,
               "action": "auto-add", "pr_number": None, "detail": "synthetic"}
    acts = plan_actions([finding], fm)
    check("T4b-exempt-repo-add-refused", not acts, json.dumps(acts))

    # 4c. round-two F1: exempt repo with a PRESENT guard ruleset plans no add either
    f9 = build_fixtures()["F9-exemption-violated"]
    acts = plan_actions(evaluate_state({"repos": f9[0], **f9[1]}, f9[2], NOW), f9[2])
    check("T4c-exemption-violation-plans-no-mutation", not acts, json.dumps(acts))

    # 5. tampered manifest = infra (validate_manifest catches)
    bad = dict(fm)
    bad["estate"] = {"org": ORG, "code_repos": CODE_REPOS[:5], "content_repos": CONTENT_REPOS}
    problems = validate_manifest(bad)
    check("T5-tampered-manifest-caught", any("code_repos mismatch" in p for p in problems), str(problems))

    # 6. README brand-token boundary
    check("T6-token-match-case-insensitive", bool(DESCRIBING_README_RE.search("ZAPPLY board infra")))
    check("T6-token-no-false-match", not DESCRIBING_README_RE.search("apply to jobs; zucchini recipes"))

    # 6b. round-two F1/F2: waiver/exemption rows must be structurally sound
    bad_waiver = dict(fm)
    bad_waiver["visibility_waivers"] = {"jobs-data-2026": {"expected_during_waiver": "public"}}
    problems = validate_manifest(bad_waiver)
    check("T6b-redundant-waiver-caught", any("redundant" in p for p in problems), str(problems))
    bad_exempt = dict(fm)
    bad_exempt["ruleset_exemptions"] = {"resume-samples-2026": {"until": "x"}}
    problems = validate_manifest(bad_exempt)
    check("T6b-content-repo-exemption-caught", any("only code repos" in p for p in problems), str(problems))

    # 6c. run-1 live bug pin: real header value is "2026-11-30 17:40:22 UTC" (spaced,
    # UTC suffix) — the \S+ capture crashed aware/naive subtraction on run 43658142137
    live = _parse_expiry("2026-11-30 17:40:22 UTC")
    check("T6c-parse-live-header-shape",
          live == datetime(2026, 11, 30, 17, 40, 22, tzinfo=timezone.utc), repr(live))
    check("T6c-parse-compact-shape",
          _parse_expiry("2026-11-30T17:40:22Z") == live, repr(_parse_expiry("2026-11-30T17:40:22Z")))
    check("T6c-parse-garbage-is-none", _parse_expiry("not-a-date") is None)

    # 7. summary renders for every verdict shape without crashing
    for name, (repos, extra, manifest, _) in build_fixtures().items():
        state = {"repos": repos, **extra}
        findings = evaluate_state(state, manifest, NOW)
        s = render_summary("selftest", verdict_of(findings, [], []), findings, [], [], state, manifest, NOW)
        if "Verdict" not in s or "|" not in s:
            check(f"T7-summary-{name}", False, "missing table")
    check("T7-summary-renders", not any(f.startswith("T7-") for f in failures))

    log("")
    if failures:
        log(f"SELFTEST RESULT: FAIL ({len(failures)}): {failures}")
        return 1
    log("SELFTEST RESULT: PASS — all detections fire on planted violations; clean baselines zero-FP; payloads pinned")
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv):
    artifacts_dir = os.environ.get("ESTATE_ARTIFACTS_DIR") or "/tmp/estate-exposure-artifacts"
    dry_run = "--dry-run" in argv
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")

    if "--selftest" in argv:
        rc = selftest()
        return rc

    if "--scan" not in argv:
        print(__doc__)
        return 2

    now = datetime.now(timezone.utc)
    Path(artifacts_dir).mkdir(parents=True, exist_ok=True)

    # designated-state baseline (fail-closed on unreadable/tampered manifest)
    try:
        manifest = json.loads(MANIFEST_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"::error::manifest unreadable at {MANIFEST_PATH}: {e}")
        return 2
    problems = validate_manifest(manifest)
    if problems:
        for p in problems:
            print(f"::error::manifest drift: {p}")
        return 2

    state, infra = collect_live_state()
    findings = evaluate_state(state, manifest, now)
    actions = plan_actions(findings, manifest)
    receipts = apply_actions(actions, state, dry_run)
    verdict = verdict_of(findings, receipts, infra)
    mode = "scan-dry-run" if dry_run else "scan"

    summary = render_summary(mode, verdict, findings, receipts, infra, state, manifest, now)
    (Path(artifacts_dir) / "summary.md").write_text(summary)
    (Path(artifacts_dir) / "verdict.txt").write_text(verdict + "\n")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(summary)
    write_findings_artifact(Path(artifacts_dir) / "findings.json", mode, verdict, findings, receipts, infra, state)

    print(f"estate-exposure {mode}: verdict={verdict} findings={len(findings)} "
          f"actions={len(actions)} infra={len(infra)}")
    for f in findings:
        print(f"  FINDING {f['id']} [{f['severity']}] {_disposition(f, receipts)}")
    for i in infra:
        print(f"  INFRA {i}")

    if verdict == "INFRA":
        print("::error::estate-exposure INFRA — coverage hole; the estate is not fully probed (fail-closed)")
        return 2
    if verdict == "ALERT":
        print("::error::estate-exposure ALERT — unfixable findings remain; this failing run IS the alert")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
