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
  open-pr            open PR on a code repo            -> AUTO-CLOSE (silent: no comment,
                     no Discord; the closure event itself notifies the PR author)
  ruleset-missing    guard-required-main ruleset absent-> AUTO-ADD (template below)
  ruleset-drift      guard ruleset present but off-template (inactive, no guard
                     context, not main, bypass actors, strict=true) -> RECORD ONLY
  ruleset-duplicate  >1 guard ruleset on main          -> RECORD ONLY
  describing-readme  README on a public code repo containing the brand token
                     ("zapply", case-insensitive)      -> AUTO-STUB (one-line neutral
                     replacement; the FULL original text is preserved in the receipt.
                     NOTE: the old blob remains reachable in git history until a
                     history rewrite, which is an estate-ops decision — recorded in
                     the receipt, not hidden)
  content-visibility content repo visibility drift     -> RECORD ONLY (by design)
  token-expiry       GH_PAT expires in <14d (header probe) -> RECORD ONLY

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
    "ruleset-drift": "medium",
    "ruleset-duplicate": "medium",
    "open-pr": "medium",
    "content-visibility": "medium",
    "token-expiry": "medium",
}

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "estate-visibility-manifest.txt"


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

    # (f) token expiry
    tok = state.get("token_expiry") or {}
    expires = tok.get("expires")
    if expires:
        exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        days = (exp_dt - now).total_seconds() / 86400.0
        if days < TOKEN_EXPIRY_WARN_DAYS:
            findings.append({
                "id": "token-expiry:gh-pat",
                "class": "token-expiry",
                "repo": "-",
                "severity": SEVERITY["token-expiry"],
                "fixable": False,
                "action": "record-only",
                "detail": f"GH_PAT expires {expires} ({days:.1f}d < {TOKEN_EXPIRY_WARN_DAYS:.0f}d) — renew at github.com/settings/personal-access-tokens; header-probe receipt in findings artifact",
                "days_remaining": round(days, 2),
            })

    repos = state.get("repos") or {}
    for repo, estate in _manifest_repos(manifest).items():
        rs = repos.get(repo) or {}
        cls = "code" if estate["class"] == "code" else "content"

        if not rs.get("reachable"):
            # collect_live_state already escalated to infra; nothing here (fail-closed
            # at the exit-code layer, not as a finding class).
            continue

        # (a)/(e) visibility vs designated baseline
        expected = estate["expected_visibility"]
        actual = rs.get("visibility")
        if actual != expected:
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

        # (b) open PRs -> auto-close
        for pr in rs.get("open_prs") or []:
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

        # (c) guard ruleset
        guard_rulesets = [r for r in (rs.get("rulesets") or []) if ruleset_guard_status(r) in ("canonical", "drifted")]
        canonical = [r for r in guard_rulesets if ruleset_guard_status(r) == "canonical"]
        if not guard_rulesets:
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
            if len(canonical) == 0:
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
                    "detail": "README describes the operation (brand token match) — auto-stub to one-line neutral; original preserved in receipt; old blob stays in git history until estate-ops history rewrite",
                })

    findings.sort(key=lambda f: (f["repo"], f["class"], f["id"]))
    return findings


def plan_actions(findings):
    """Pure: findings -> mutation plan (only fixable classes; payload per class)."""
    actions = []
    for f in findings:
        if f["class"] == "open-pr":
            actions.append({
                "finding": f["id"], "type": "close-pr", "repo": f["repo"],
                "payload": {"pr_number": f["pr_number"]},
            })
        elif f["class"] == "ruleset-missing":
            actions.append({
                "finding": f["id"], "type": "add-ruleset", "repo": f["repo"],
                "payload": dict(RULESET_BODY),
            })
        elif f["class"] == "describing-readme":
            actions.append({
                "finding": f["id"], "type": "stub-readme", "repo": f["repo"],
                "payload": {
                    "path": "README.md",
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
            out_i, _ = gh(["api", f"repos/{ORG}/{repo}", "-i"])
            m = re.search(r"(?im)^github-authentication-token-expiration:\s*(\S+)", out_i)
            if m and state["token_expiry"] is None:
                state["token_expiry"] = {"expires": m.group(1), "probed_on": repo}
            meta = _gh_json(["api", f"repos/{ORG}/{repo}"])
            rs.update(reachable=True, visibility=meta.get("visibility"),
                      archived=bool(meta.get("archived")),
                      default_branch=meta.get("default_branch") or "main")
            if not rs["archived"]:
                if repo in CODE_REPOS:
                    prs = _gh_json(["api", f"repos/{ORG}/{repo}/pulls?state=open&per_page=100"])
                    rs["open_prs"] = [{"number": p["number"], "title": (p.get("title") or "")[:120],
                                       "author": ((p.get("user") or {}).get("login") or "?")}
                                      for p in prs]
                    # rulesets list then detail-fetch candidates (list omits rules)
                    summaries = _gh_json(["api", f"repos/{ORG}/{repo}/rulesets"]) or []
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
    """README text + sha via contents API. 404/absent -> present=False."""
    try:
        out, _ = gh(["api", f"repos/{ORG}/{repo}/contents/README.md?ref={default_branch}"])
        meta = json.loads(out)
        content = meta.get("content") or ""
        if not content:
            infra.append(f"{repo}: README.md >1MB via contents API — unprobeable (fail-closed)")
            return {"present": True, "text": None, "sha": meta.get("sha")}
        text = base64.b64decode(content).decode("utf-8", "replace")
        return {"present": True, "text": text, "sha": meta.get("sha")}
    except GhError as e:
        if "404" in str(e) or "Not Found" in str(e):
            return {"present": False, "text": None, "sha": None}
        infra.append(f"{repo}: README probe failed (fail-closed): {e}")
        return {"present": True, "text": None, "sha": None}


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
                proc = subprocess.run(
                    ["gh", "api", f"repos/{ORG}/{a['repo']}/contents/README.md", "-X", "PUT",
                     "--input", "-", "--jq", ".commit.sha"],
                    input=body, capture_output=True, text=True,
                    env={**os.environ, "GH_TOKEN": os.environ.get("GH_TOKEN", "")}, timeout=60)
                if proc.returncode != 0:
                    raise GhError(proc.stderr.strip()[:400])
                orig = ((state.get("repos", {}).get(a["repo"]) or {}).get("readme") or {}).get("text") or ""
                rec["result"] = "applied"
                rec["detail"] = f"README stubbed (commit {proc.stdout.strip()[:12]}); original preserved below"
                rec["original_readme"] = orig
            else:
                raise GhError(f"unknown action type {a['type']}")
        except GhError as e:
            rec["result"] = "failed"
            rec["detail"] = f"auto-fix failed (escalated): {e}"
        receipts.append(rec)
    return receipts


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
    for repo in ESTATE_REPOS:
        rs = repos.get(repo) or {}
        vis = rs.get("visibility") or "UNREACHABLE"
        probes = []
        if rs.get("reachable"):
            if repo in CODE_REPOS and not rs.get("archived"):
                probes.append(f"prs={len(rs.get('open_prs') or [])}")
                probes.append(f"rulesets={len(rs.get('rulesets') or [])}")
                probes.append("readme=yes" if (rs.get("readme") or {}).get("present") else "readme=absent")
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
            "expected_visibility": expected, "record_only": record_only}


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
    """Named fixture states. Deterministic; NOW pinned. Each = (name, state, expected_ids)."""
    fm = _fixture_manifest()
    F = {}

    # F1 (a): fake public-expected code repo that is actually private -> record-only
    F["F1-code-visibility"] = (
        {r: _probed_code_state() for r in CODE_REPOS}
        | {r: _repo_state() for r in CONTENT_REPOS}
        | {"jobs-data-2026": _probed_code_state(visibility="private")},
        {"token_expiry": {"expires": "2026-12-30T00:00:00Z"}}, fm,
        {"code-visibility:jobs-data-2026"},
    )

    # F2 (b): planted open PR -> auto-close plan
    state2 = {r: _probed_code_state() for r in CODE_REPOS}
    state2.update({r: _repo_state() for r in CONTENT_REPOS})
    state2["job-board-aggregator"] = _repo_state(
        open_prs=[{"number": 5, "title": "Bump actions/checkout from 5 to 6", "author": "dependabot[bot]"}],
        rulesets=[_canonical_ruleset()], readme={"present": True, "text": "Job listings pipeline.\n", "sha": "abc"})
    F["F2-open-pr"] = (state2, {"token_expiry": {"expires": "2026-12-30T00:00:00Z"}}, fm,
                       {"open-pr:job-board-aggregator:5"})

    # F3 (c): missing ruleset -> auto-add (only on that repo)
    state3 = {r: _probed_code_state() for r in CODE_REPOS}
    state3["jobs-data-2026"] = _probed_code_state(rulesets=[])
    state3.update({r: _repo_state() for r in CONTENT_REPOS})
    F["F3-ruleset-missing"] = (state3, {"token_expiry": {"expires": "2026-12-30T00:00:00Z"}}, fm,
                               {"ruleset-missing:jobs-data-2026"})

    # F3b: guard ruleset present but enforcement=disabled -> drift (record-only, NO add)
    drifted = _canonical_ruleset(id=777, name="guard-required-main", enforcement="disabled")
    state3b = {r: _probed_code_state() for r in CODE_REPOS}
    state3b["job-board-shared"] = _probed_code_state(rulesets=[drifted])
    state3b.update({r: _repo_state() for r in CONTENT_REPOS})
    F["F3b-ruleset-drift"] = (state3b, {"token_expiry": {"expires": "2026-12-30T00:00:00Z"}}, fm,
                              {"ruleset-drift:job-board-shared"})

    # F3c: canonical + unknown extra fields -> NO finding (tolerant predicate; first-run FP guard)
    tolerant = _canonical_ruleset(id=999)
    tolerant["rules"][0]["parameters"]["extra_unknown"] = {"whatever": 1}
    tolerant["some_future_field"] = "ignored"
    state3c = {r: _probed_code_state() for r in CODE_REPOS}
    state3c["jobs-aggregator-engine"] = _probed_code_state(rulesets=[tolerant])
    state3c.update({r: _repo_state() for r in CONTENT_REPOS})
    F["F3c-ruleset-tolerant"] = (state3c, {"token_expiry": {"expires": "2026-12-30T00:00:00Z"}}, fm, set())

    # F4 (d): describing README -> auto-stub; F4b neutral -> none
    state4 = {r: _probed_code_state() for r in CODE_REPOS}
    state4["jobs-data-2026"] = _probed_code_state(
        readme={"present": True, "sha": "deadbeef",
                "text": "Part of the [Zapply](https://zapply.jobs) job board infrastructure. See [zapplyjobs](https://github.com/zapplyjobs) for the public job boards.\n"})
    state4.update({r: _repo_state() for r in CONTENT_REPOS})
    F["F4-describing-readme"] = (state4, {"token_expiry": {"expires": "2026-12-30T00:00:00Z"}}, fm,
                                 {"describing-readme:jobs-data-2026"})

    # F5 (e): content repo private -> record-only; code side fully clean
    state5 = {r: _probed_code_state() for r in CODE_REPOS}
    state5.update({r: _repo_state() for r in CONTENT_REPOS})
    state5["resume-samples-2026"] = _repo_state(visibility="private")
    F["F5-content-visibility"] = (state5, {"token_expiry": {"expires": "2026-12-30T00:00:00Z"}}, fm,
                                  {"content-visibility:resume-samples-2026"})

    # F6 (f): token expiry boundary — 13.9d fires; 14.0d does not; 30d does not
    base = {r: _probed_code_state() for r in CODE_REPOS}
    base.update({r: _repo_state() for r in CONTENT_REPOS})
    F["F6a-token-expiry-fires"] = (dict(base), {"token_expiry": {"expires": "2026-11-28T02:24:00Z"}}, fm,
                                   {"token-expiry:gh-pat"})  # NOW + 13.9d
    F["F6b-token-expiry-boundary-clean"] = (dict(base), {"token_expiry": {"expires": "2026-11-28T12:00:01Z"}}, fm,
                                            set())  # NOW + 14.000d

    # F7: clean baseline — zero findings, zero actions
    clean = {r: _probed_code_state(readme={"present": True, "text": "Docs for the public job listings pipeline.\n", "sha": "x"})
             for r in CODE_REPOS}
    clean.update({r: _repo_state() for r in CONTENT_REPOS})
    F["F7-clean-baseline"] = (clean, {"token_expiry": {"expires": "2026-12-14T12:00:00Z"}}, fm, set())

    # F8: clean baseline + fully absent readmes + no rulesets probed on content repos
    # (content repos carry open_prs=None etc. — evaluator must not probe-fall-back)
    clean2 = {r: _probed_code_state(readme={"present": False, "text": None, "sha": None})
              for r in CODE_REPOS}
    clean2.update({r: {"reachable": True, "visibility": "public", "archived": False,
                       "default_branch": "main", "open_prs": None, "rulesets": None, "readme": None}
                   for r in CONTENT_REPOS})
    F["F8-clean-no-readme"] = (clean2, {"token_expiry": {"expires": "2026-12-14T12:00:00Z"}}, fm, set())

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
    for name in ("F7-clean-baseline", "F8-clean-no-readme", "F3c-ruleset-tolerant", "F6b-token-expiry-boundary-clean"):
        repos, extra, manifest, expected_ids = build_fixtures()[name]
        state = {"repos": repos, **extra}
        actions = plan_actions(evaluate_state(state, manifest, NOW))
        check(f"T2-{name}-zero-actions", not actions, f"planned {actions}")

    # 3. action-plan payload pins
    f2 = build_fixtures()["F2-open-pr"]
    acts = plan_actions(evaluate_state({"repos": f2[0], **f2[1]}, f2[2], NOW))
    check("T3-close-pr-payload", acts and acts[0]["type"] == "close-pr"
          and acts[0]["payload"] == {"pr_number": 5}, json.dumps(acts))

    f3 = build_fixtures()["F3-ruleset-missing"]
    acts = plan_actions(evaluate_state({"repos": f3[0], **f3[1]}, f3[2], NOW))
    ok = (len(acts) == 1 and acts[0]["type"] == "add-ruleset"
          and json.dumps(acts[0]["payload"], sort_keys=True) == json.dumps(RULESET_BODY, sort_keys=True))
    check("T3-add-ruleset-body-pinned-to-template", ok, json.dumps(acts))

    f4 = build_fixtures()["F4-describing-readme"]
    acts = plan_actions(evaluate_state({"repos": f4[0], **f4[1]}, f4[2], NOW))
    stub = [a for a in acts if a["type"] == "stub-readme"]
    ok = (len(stub) == 1
          and base64.b64decode(stub[0]["payload"]["content_b64"]).decode() == NEUTRAL_STUB
          and stub[0]["payload"]["path"] == "README.md")
    check("T3-stub-readme-payload", ok, json.dumps(acts))

    # 4. boundary: drifted ruleset must NOT plan an add (machinery never creates its own duplicate)
    f3b = build_fixtures()["F3b-ruleset-drift"]
    acts = plan_actions(evaluate_state({"repos": f3b[0], **f3b[1]}, f3b[2], NOW))
    check("T4-drift-plans-no-add", not acts, json.dumps(acts))

    # 5. tampered manifest = infra (validate_manifest catches)
    bad = dict(fm)
    bad["estate"] = {"org": ORG, "code_repos": CODE_REPOS[:5], "content_repos": CONTENT_REPOS}
    problems = validate_manifest(bad)
    check("T5-tampered-manifest-caught", any("code_repos mismatch" in p for p in problems), str(problems))

    # 6. README brand-token boundary
    check("T6-token-match-case-insensitive", bool(DESCRIBING_README_RE.search("ZAPPLY board infra")))
    check("T6-token-no-false-match", not DESCRIBING_README_RE.search("apply to jobs; zucchini recipes"))

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
    actions = plan_actions(findings)
    receipts = apply_actions(actions, state, dry_run)
    verdict = verdict_of(findings, receipts, infra)
    mode = "scan-dry-run" if dry_run else "scan"

    summary = render_summary(mode, verdict, findings, receipts, infra, state, manifest, now)
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(summary)
    else:
        (Path(artifacts_dir) / "summary.md").write_text(summary)
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
