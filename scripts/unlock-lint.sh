#!/usr/bin/env bash
# unlock-lint - INF-TRANSCRYPT-CRYPTPATH-GUARD-1
# Fails if any workflow invokes node while lacking transcrypt unlock references.
# Rationale: *.js/*.json/*.jsonl are filter:crypt - a workflow that runs them
# without an unlock step guarantees a runtime SyntaxError on the runner
# (silent-failure class, incident 2026-09-13, run 34731392382).
# NOTE: workflow files themselves are plaintext (.yml is not in .gitattributes),
# so this lint needs no unlock. Keep it that way.
set -u
fail=0
for f in .github/workflows/*.yml .github/workflows/*.yaml; do
  [ -f "$f" ] || continue
  runs_node=$(grep -Eq '(^|[[:space:]])(node|npx)([[:space:]]|$)|node +[^|]*\.js' "$f" && echo 1 || echo 0)
  has_tc=$(grep -qi transcrypt "$f" && echo 1 || echo 0)
  if [ "$runs_node" = "1" ] && [ "$has_tc" = "0" ]; then
    echo "FAIL: $f invokes node but has NO transcrypt unlock - runtime SyntaxError guaranteed on checkout without the key (INF-TRANSCRYPT-CRYPTPATH-GUARD-1)"
    fail=1
  fi
done
[ "$fail" = "0" ] && echo "unlock-lint: all workflows OK"
exit $fail
