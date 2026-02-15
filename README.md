# Jobs Aggregator (Private)

**Private repository containing job aggregation and filtering logic for Zapply.**

> ⚠️ **PRIVATE REPOSITORY** - Contains proprietary business logic, company lists, and filtering strategies.

---

## Overview

Centralized job aggregator that fetches from multiple sources, validates data quality, filters senior positions, and publishes a unified feed.

**Pipeline:** Fetch → Validate → Filter → Tag → Deduplicate → Sort → Write

**Output:** `all_jobs.json` (~12 MB, 1,000+ entry-level jobs)

---

## Architecture

### Data Sources
1. **JSearch API** - Broad job search (paid tier)
2. **ATS Direct** - 27 tech companies via Greenhouse, Lever, Ashby APIs

### Processing Pipeline

**Step 1: Fetch** (~500-800 jobs total)
- JSearch: "Software Engineer" + "New Grad" + other keywords
- ATS: Direct API calls to 27 company boards

**Step 2: Validate & Normalize** (~300-600 jobs, filter ~30%)
- **Required:** title (5+ chars), company (2+ chars), URL (http prefix)
- **Auto-fix:** Extract state/city from location field
- **Skip:** Malformed jobs with missing required fields

**Step 2.5: Filter Senior Jobs** (~200-400 jobs, filter ~20-30%)
- **Title keywords:** senior, sr., lead, principal, staff, manager, director, vp, chief, architect, etc.
- **Experience threshold:** 5+ years required → filtered
- **Conservative:** When in doubt, include the job

**Step 2.75: Apply Tags** (skill extraction)
- Frontend, Backend, Full-Stack, Mobile, DevOps, Data, AI/ML, etc.

**Step 3: Deduplicate** (company + title similarity)

**Step 4: Sort** (posted_at DESC)

**Step 5: Write Output** (all_jobs.json + metadata.json)

---

## Key Files

### Fetchers (`fetchers/`)
- `jsearch-fetcher.js` - JSearch API integration
- `greenhouse.js` - Greenhouse ATS client (18 companies)
- `lever.js` - Lever ATS client (4 companies)
- `ashby.js` - Ashby ATS client (5 companies)
- `ats-fetcher.js` - ATS coordinator
- `company-list.json` - **SENSITIVE** - Curated company list

### Processors (`processors/`)
- `validator.js` - 3-tier validation (REQUIRED/AUTO-FIX/OPTIONAL)
- `senior-filter.js` - Title + experience-based filtering
- `tag-engine.js` - Skill tag extraction
- `deduplicator.js` - Similarity-based deduplication
- `normalizer.js` - Location/field normalization

### Main
- `index.js` - Pipeline orchestrator
- `utils/logger.js` - Logging utilities

---

## Company List (Sensitive)

**27 tech companies across 3 ATS platforms:**

- **Greenhouse (18):** stripe, airbnb, figma, notion, ramp, canva, discord, webflow, etc.
- **Lever (4):** netflix, shopify, reddit, grammarly
- **Ashby (5):** plaid, anthropic, brex, databricks, rippling

> ⚠️ **Strategic Selection** - This list represents competitive intelligence and should remain private.

---

## Filtering Logic (Sensitive)

### Senior Keywords (16 total)
```
senior, sr., sr, lead, principal, staff
manager, director, vp, vice president, chief
cto, cio, ceo, head of, architect
```

### Experience Patterns
- "5+ years", "7 years" → filtered
- "5-7 years" → filtered (uses minimum)
- "3-5 years" → **NOT** filtered (edge case fix)
- "minimum 5 years", "at least 6 years" → filtered

> ⚠️ **Business Rules** - These thresholds are tuned for entry-level focus and should remain private.

---

## Usage

### Local Testing
```bash
# Dry run (no file writes)
node .github/scripts/index.js --dry-run

# Full run
node .github/scripts/index.js
```

### GitHub Actions
```bash
# Manual trigger
gh workflow run fetch-jobs.yml

# Scheduled: Every 15 minutes (configurable)
```

### Environment Variables
- `JSEARCH_API_KEY` - Required for JSearch API (paid tier)

---

## Output Format

### all_jobs.json
```json
[
  {
    "id": "unique-id",
    "source": "greenhouse|lever|ashby|jsearch",
    "title": "Software Engineer",
    "company_name": "Example Corp",
    "url": "https://...",
    "location": "San Francisco, CA",
    "job_city": "San Francisco",
    "job_state": "CA",
    "posted_at": "2026-02-15T12:00:00Z",
    "tags": ["backend", "python"],
    "employment_type": "Full-time"
  }
]
```

### metadata.json
```json
{
  "last_updated": "2026-02-15T12:00:00Z",
  "total_jobs": 1234,
  "by_source": { "jsearch": 800, "greenhouse": 300, "lever": 100, "ashby": 34 },
  "validation_stats": {
    "total_input": 800,
    "valid_jobs": 600,
    "invalid_jobs": 200
  },
  "senior_filter_stats": {
    "total_input": 600,
    "entry_level": 400,
    "senior_filtered": 200
  },
  "tag_stats": { "frontend": 150, "backend": 200, "fullstack": 50 }
}
```

---

## Deployment Strategy

**Recommended: Option C - Direct Fetch**

1. **Aggregator runs independently** (jobs-data-2026 repo, GitHub Actions)
2. **Publishes to private repo:** `all_jobs.json` + `metadata.json`
3. **Consumer repos fetch from private repo:**
   ```bash
   # Fetch with GitHub token
   curl -H "Authorization: token $GITHUB_TOKEN" \
     https://raw.githubusercontent.com/zapplyjobs/jobs-aggregator-private/main/all_jobs.json
   ```

**Why Option C:**
- Simple, decoupled architecture
- No submodule complexity
- Clear producer/consumer separation
- Matches centralized aggregator design

---

## Security Considerations

### What's Sensitive
1. **Company Lists** - Strategic curation (competitive intelligence)
2. **Filtering Rules** - Business logic for entry-level focus
3. **API Integration Patterns** - Rate limiting, error handling strategies
4. **Validation Thresholds** - Quality standards and auto-fix logic

### Access Control
- ✅ Repository is **PRIVATE** (not public)
- ✅ Only zapplyjobs organization members can access
- ⚠️ Consumer repos need **GitHub token** with `repo` scope to fetch output

### GitHub Actions Secrets
- `JSEARCH_API_KEY` - Configured in repository secrets
- `GITHUB_TOKEN` - Auto-provided by GitHub Actions (for private repo access)

---

## Testing

### Validation Tests (7 test cases)
- ✅ Valid jobs with all required fields
- ✅ Missing title → filtered
- ✅ Missing company → filtered
- ✅ Invalid URL → filtered
- ✅ Location parsing: "San Francisco, CA" → city + state

### Senior Filter Tests (12 test cases)
- ✅ Entry-level: "Software Engineer" → included
- ✅ Senior title: "Senior Engineer" → filtered
- ✅ Senior experience: "5+ years" → filtered
- ✅ Edge case: "3-5 years" → **NOT** filtered (bug fixed)

**All tests: 100% accuracy**

---

## Metrics Tracking

Each pipeline step tracks metrics:
- **Fetch:** Jobs per source, API usage stats
- **Validation:** Valid/invalid counts, normalization stats
- **Senior Filter:** Entry-level/senior counts, filter reasons
- **Tagging:** Tag distribution, coverage
- **Deduplication:** Duplicate count, similarity scores

Metrics written to `metadata.json` for monitoring.

---

## Maintenance

### Update Company List
Edit `fetchers/company-list.json` with new companies.

### Adjust Senior Filter
Edit `processors/senior-filter.js`:
- `SENIOR_KEYWORDS` - Add/remove title keywords
- `MIN_SENIOR_YEARS` - Change experience threshold

### Modify Validation Rules
Edit `processors/validator.js`:
- `isValidJob()` - Change required fields
- `normalizeJob()` - Add auto-fix logic

---

## Performance

**Expected metrics:**
- **Execution time:** ~2-3 minutes
- **API calls:** ~30-40 (27 ATS + 1-3 JSearch)
- **Rate limiting:** 500ms between ATS requests
- **Output size:** ~12 MB (1,000+ jobs)
- **Memory usage:** <500 MB

---

## Related Repositories

- **New-Grad-Jobs-2026** (consumer) - Main job board
- **Internships-2026** (consumer) - Internships board
- **Remote-Jobs-2026** (consumer) - Remote jobs board

---

**Last Updated:** 2026-02-15
**Status:** Phase 1A/B/C Complete - Ready for Phase 1D/E/F
