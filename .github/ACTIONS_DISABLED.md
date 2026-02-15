# GitHub Actions - DISABLED

**Status:** GitHub Actions are **DISABLED** for this private repository.

## Why Disabled?

This is a **code repository only** - it stores aggregator source code but does not run workflows itself.

The actual aggregator execution happens in the **jobs-data-2026** public repository, which:
1. Contains this code as a reference/backup
2. Runs the workflows on a schedule
3. Uses the same codebase but with different configuration

## To Re-enable Actions (if needed):

```bash
# Via GitHub CLI
gh api --method PUT /repos/zapplyjobs/jobs-aggregator-private/actions/permissions -F enabled=true

# Or via GitHub UI
# Settings → Actions → General → Actions permissions → Allow all actions
```

## Workflow Status

- ❌ All workflows disabled via API
- ✅ Old Discord workflows removed (collect-metrics, health-check, post-to-discord, verify-discord)
- ⏸️ fetch-jobs.yml.disabled remains (not active, kept for reference)

---

**Last Updated:** 2026-02-16
**Disabled By:** Claude (user request)
