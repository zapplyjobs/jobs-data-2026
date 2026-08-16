#!/usr/bin/env python3
"""Dependency-resolution gate check — INF-DEPRESOLUTION-ANALYZER-1.

Blocks the class where a workflow executes a script whose external require() was
satisfied by an install tree that a manifest/install change silently removed.
Struck 3x in 2 days (2026-08-15/16): system-state's silent 19h upload failure
(@aws-sdk gone with the root manifest), verify-all-jobs + source-coverage-audit
(NODE_PATH pointing at shared/, which has no package.json).

Design per the validated prototype in
projects/zjp/research/DEP_RESOLUTION_CI_CHECK_2026_08_15.md:
  - parse every workflow step's run block
  - extract executions: `node <script>` (cd-aware), `NODE_PATH=... node ...`,
    inline `node -e` / heredoc `node <<EOF` bodies
  - follow RELATIVE requires transitively (decrypted tree required — run after
    the transcrypt unlock step)
  - for each external require in file F: satisfied iff the dep is declared in a
    manifest of an INSTALLED ancestor tree of F, or in a NODE_PATH tree manifest
    (per-requiring-file resolution — the prototype's over-approximation, fixed)
  - Node builtins allowlisted; Python steps are a non-goal (runner-provided)

Run: python3 .github/scripts/dep-resolution-check.py   (exit 0 = clean, 1 = findings)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "console", "constants", "crypto",
    "dgram", "dns", "domain", "events", "fs", "http", "http2", "https", "inspector",
    "module", "net", "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "timers", "tls", "trace_events",
    "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
}
REQUIRE_RE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
EXPLICIT_NODE_RE = re.compile(r"""(?:^|\s|&&|;|\()node\s+([\w./@$-]+\.js)\b""")
NODE_E_RE = re.compile(r"""node\s+(?:-e\s+)?['"](.+)['"]\s*$""")
INSTALL_RE = re.compile(r"""(?:npm\s+(?:ci|install)|\bnpm\b.*\b(?:ci|install)\b)""")
CD_RE = re.compile(r"^\s*cd\s+(\S+)")


def is_external(spec: str) -> bool:
    if spec.startswith(".") or spec.startswith("/"):
        return False
    return True


def pkg_name(spec: str) -> str:
    # @scope/name/subpath -> @scope/name ; name/subpath -> name
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2])
    return parts[0]


def manifest_deps(tree: Path) -> set[str] | None:
    pj = tree / "package.json"
    if not pj.is_file():
        return None
    try:
        data = yaml.safe_load(pj.read_text()) or {}
    except Exception:
        return None
    deps = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        deps.update((data.get(key) or {}).keys())
    return deps


def requires_of(path: Path) -> list[tuple[str, Path | None]]:
    """(spec, resolved_file_or_None) for every require in the file (best-effort)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    out = []
    for spec in REQUIRE_RE.findall(text):
        if not is_external(spec):
            target = (path.parent / spec).resolve()
            for candidate in (target, Path(str(target) + ".js"), target / "index.js"):
                if candidate.is_file():
                    out.append((spec, candidate))
                    break
            else:
                out.append((spec, None))
        else:
            out.append((spec, None))
    return out


def satisfied(spec: str, requiring_file: Path, installed_trees: dict[Path, set[str]],
              nodepath_trees: list[Path]) -> bool:
    name = pkg_name(spec)
    if name in NODE_BUILTINS:
        return True
    trees = list(installed_trees) + [t for t in nodepath_trees if t not in installed_trees]
    for tree, deps in installed_trees.items():
        try:
            tree.relative_to(REPO)
        except ValueError:
            continue
        # ancestor-of-requiring-file resolution
        try:
            requiring_file.relative_to(tree)
        except ValueError:
            if tree not in nodepath_trees:
                continue
        if name in deps:
            return True
    for tree in nodepath_trees:
        deps = manifest_deps(tree)
        if deps and name in deps:
            return True
    return False


def extract_executions(run: str, wf_dir: Path) -> list[dict]:
    """Walk a run block line-by-line, tracking cwd; yield node executions."""
    execs = []
    cwd = ""
    lines = run.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = CD_RE.match(line)
        if m:
            cwd = norm_target(m.group(1))
            i += 1
            continue
        nodepath = None
        npm = re.search(r"NODE_PATH=\$\{?GITHUB_WORKSPACE\}?/(\S+?)/node_modules", line)
        if not npm:
            npm = re.search(r"NODE_PATH=(\S+?)/node_modules", line)
        if npm:
            nodepath = npm.group(1)
        # heredoc node <<'EOF' ... EOF
        heredoc = re.search(r"node\s*<<\s*['\"]?(\w+)['\"]?", line)
        if heredoc:
            tag = heredoc.group(1)
            body = []
            i += 1
            while i < len(lines) and lines[i].strip() != tag:
                body.append(lines[i])
                i += 1
            execs.append({"cwd": cwd, "nodepath": nodepath, "inline_js": "\n".join(body)})
            i += 1
            continue
        # node -e '...' / node -e "..." (single line)
        inline = re.search(r"node\s+-e\s+(.+)$", line)
        if inline:
            execs.append({"cwd": cwd, "nodepath": nodepath, "inline_js": inline.group(1)})
            i += 1
            continue
        # node script.js
        m = EXPLICIT_NODE_RE.search(line)
        if m:
            execs.append({"cwd": cwd, "nodepath": nodepath, "script": m.group(1)})
        i += 1
    return execs

def norm_target(target: str) -> str:
    """Normalize a shell path target: strip quotes and $GITHUB_WORKSPACE prefixes."""
    target = re.sub(r"[\"']", "", target).strip()  # quotes never legitimately appear mid-path
    target = re.sub(r"\$\{?GITHUB_WORKSPACE\}?(?=/|$)", "", target)
    return target.lstrip("/")  # never let a normalized target look absolute (it is repo-relative)

def workflow_installed_trees(run: str) -> dict[Path, set[str]]:
    """Trees this workflow installs (cd X && npm install/ci, or working-directory steps).
    Satisfying set = the tree's LOCKFILE packages when present (npm ci/install materializes
    the whole lockfile tree — transitive deps like `he` are installable without being
    direct manifest deps), else the manifest's direct deps."""
    trees: dict[Path, set[str]] = {}
    lines = run.splitlines()
    cwd = ""
    for line in lines:
        m = CD_RE.match(line)
        if m:
            cwd = m.group(1).strip('"')
            # a cd-line may ALSO carry the install (`cd X && npm install`) — fall through
        if INSTALL_RE.search(line):
            inline_cd = re.search(r"cd\s+(\S+)\s*(?:;|&&)\s*npm", line)
            target = norm_target(inline_cd.group(1)) if inline_cd else norm_target(cwd)
            tree = (REPO / target).resolve()
            deps = tree_deps(tree)
            if deps is not None:
                trees[tree] = deps
    return trees


def tree_deps(tree: Path) -> set[str] | None:
    """Everything `npm install` would place in tree/node_modules: lockfile package
    keys when a lockfile exists, else direct manifest deps."""
    lock = tree / "package-lock.json"
    if lock.is_file():
        try:
            data = yaml.safe_load(lock.read_text()) or {}
            pkgs = set()
            for key in (data.get("packages") or {}):
                if key.startswith("node_modules/"):
                    pkgs.add(key[len("node_modules/"):].split("/node_modules/")[0])
            if pkgs:
                return pkgs
        except Exception:
            pass
    return manifest_deps(tree)


def main() -> int:
    findings: list[str] = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        try:
            doc = yaml.safe_load(wf.read_text())
        except Exception as exc:
            findings.append(f"{wf.name}: unparseable YAML ({exc})")
            continue
        jobs = (doc or {}).get("jobs") or {}
        installed: dict[Path, set[str]] = {}
        executions: list[tuple[str, dict]] = []
        for job in jobs.values():
            for step in (job or {}).get("steps") or []:
                run = step.get("run") if isinstance(step, dict) else None
                if not run:
                    continue
                installed.update(workflow_installed_trees(run))
                for ex in extract_executions(run, wf.parent):
                    executions.append((wf.name, ex))
        for wfname, ex in executions:
            nodepath_trees = []
            if ex.get("nodepath"):
                t = (REPO / ex["nodepath"]).resolve()
                nodepath_trees.append(t)
            files: list[Path] = []
            if ex.get("script"):
                p = (REPO / ex["cwd"] / ex["script"]).resolve() if ex["cwd"] else (REPO / ex["script"]).resolve()
                if p.is_file():
                    files.append(p)
                else:
                    findings.append(f"{wfname}: executes missing script {ex['script']} (cwd={ex['cwd'] or '.'})")
            if ex.get("inline_js"):
                # treat inline body as a virtual file at the cwd location
                virtual = Path(REPO / ex["cwd"] / "__inline__.js")
                for spec in REQUIRE_RE.findall(ex["inline_js"]):
                    if is_external(spec) and not satisfied(spec, virtual, installed, nodepath_trees):
                        findings.append(
                            f"{wfname}: inline node script requires '{spec}' with no satisfying install tree "
                            f"(installed: {[str(t.relative_to(REPO)) for t in installed] or 'NONE'}, "
                            f"NODE_PATH: {[str(t.relative_to(REPO)) for t in nodepath_trees] or 'none'})")
            # transitive walk of script files
            seen: set[Path] = set()
            queue = list(files)
            while queue:
                f = queue.pop()
                if f in seen:
                    continue
                seen.add(f)
                for spec, resolved in requires_of(f):
                    if resolved is not None:
                        queue.append(resolved)
                        continue
                    if not satisfied(spec, f, installed, nodepath_trees):
                        findings.append(
                            f"{wfname}: {f.relative_to(REPO)} requires '{spec}' with no satisfying install tree "
                            f"(installed: {[str(t.relative_to(REPO)) for t in installed] or 'NONE'}, "
                            f"NODE_PATH: {[str(t.relative_to(REPO)) for t in nodepath_trees] or 'none'})")

    if findings:
        print("::error::Dependency-resolution findings — a workflow executes code whose externals are not installed:")
        for f in findings:
            print(f"::error::{f}")
        print(f"\n{len(findings)} finding(s). Fix: add/repair the workflow's npm install step or NODE_PATH target.")
        return 1
    print("✓ Dependency-resolution check clean: every workflow-executed external require is satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
