#!/usr/bin/env node

/**
 * Closed Job Cleanup (AGG-14)
 *
 * Checks Greenhouse and Lever jobs >7 days old against their public APIs.
 * If the API returns 404, the job is closed and removed from all_jobs.json.
 *
 * Design:
 *   - Batched: checks oldest BATCH_SIZE jobs per run (default 1000)
 *   - Rate-limited: DELAY_MS between API calls (default 150ms)
 *   - Safe: network errors → skip (don't remove unverifiable jobs)
 *   - Idempotent: re-running on the same pool is safe
 *
 * Run twice daily via cleanup-closed.yml. At 1000/run, full pool coverage in ~2 days.
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const JOBS_FILE = path.join(DATA_DIR, 'all_jobs.json');
const BATCH_SIZE = parseInt(process.env.BATCH_SIZE || '1000', 10);
const DELAY_MS = 150;
const MIN_AGE_DAYS = 7;

function httpGet(url) {
  return new Promise((resolve) => {
    https.get(url, { headers: { 'User-Agent': 'Zapply-Cleanup/1.0' }, timeout: 8000 }, (res) => {
      // Drain response body to free socket
      res.resume();
      resolve(res.statusCode);
    }).on('error', () => resolve(null)).on('timeout', function () { this.destroy(); resolve(null); });
  });
}

async function checkGreenhouseJob(jobId) {
  // ID format: greenhouse-{boardToken}-{ghId}
  const parts = jobId.split('-');
  if (parts.length < 3) return null;
  const boardToken = parts[1];
  const ghId = parts.slice(2).join('-');
  const status = await httpGet(`https://boards-api.greenhouse.io/v1/boards/${boardToken}/jobs/${ghId}`);
  if (status === 404) return 'closed';
  if (status === 200) return 'open';
  return null; // error or unexpected status — skip
}

async function checkLeverJob(jobId) {
  // ID format: lever-{company}-{leverId}
  const parts = jobId.split('-');
  if (parts.length < 3) return null;
  const company = parts[1];
  const leverId = parts.slice(2).join('-');
  const status = await httpGet(`https://api.lever.co/v0/postings/${company}/${leverId}`);
  if (status === 404) return 'closed';
  if (status === 200) return 'open';
  return null;
}

async function main() {
  if (!fs.existsSync(JOBS_FILE)) {
    console.log('all_jobs.json not found — nothing to clean');
    process.exit(0);
  }

  const lines = fs.readFileSync(JOBS_FILE, 'utf8').trim().split('\n').filter(Boolean);
  const now = Date.now();
  const minAgeMs = MIN_AGE_DAYS * 24 * 60 * 60 * 1000;

  // Select checkable jobs: GH + Lever, >7d old, sorted oldest first
  const checkable = [];
  const keepLines = []; // lines not being checked (kept as-is)
  const checkLineMap = new Map(); // lineIndex → job for jobs being checked

  for (let i = 0; i < lines.length; i++) {
    const job = JSON.parse(lines[i]);
    const isCheckable = (job.source === 'greenhouse' || job.source === 'lever') &&
      job.posted_at && (now - new Date(job.posted_at).getTime()) > minAgeMs;

    if (isCheckable) {
      checkable.push({ index: i, job });
    }
  }

  // Shuffle checkable list so each run samples different jobs.
  // Over multiple runs, the full pool gets covered. Avoids always checking the
  // same oldest jobs (which tend to be evergreen postings that are always open).
  for (let i = checkable.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [checkable[i], checkable[j]] = [checkable[j], checkable[i]];
  }
  const batch = checkable.slice(0, BATCH_SIZE);

  console.log(`🔍 Checking ${batch.length} jobs (${checkable.length} checkable, batch ${BATCH_SIZE})`);

  let closed = 0, open = 0, errors = 0;
  const closedIds = new Set();

  for (const { job } of batch) {
    let result;
    if (job.source === 'greenhouse') result = await checkGreenhouseJob(job.id);
    else if (job.source === 'lever') result = await checkLeverJob(job.id);

    if (result === 'closed') {
      closed++;
      closedIds.add(job.id);
    } else if (result === 'open') {
      open++;
    } else {
      errors++;
    }

    await new Promise(r => setTimeout(r, DELAY_MS));
  }

  console.log(`\n📊 Results: ${open} open, ${closed} closed, ${errors} errors`);

  // Safety: if >50% of checked jobs return 404, likely an API outage — abort
  const checked = open + closed;
  if (checked > 0 && closed / checked > 0.5) {
    console.log(`🛑 Aborting: ${closed}/${checked} (${Math.round(closed/checked*100)}%) returned 404 — likely API outage, not mass closure`);
    process.exit(0);
  }

  if (closed === 0) {
    console.log('✅ No closed jobs found — pool unchanged');
    process.exit(0);
  }

  // Rewrite all_jobs.json without closed jobs
  const remaining = lines.filter(line => {
    try {
      const job = JSON.parse(line);
      return !closedIds.has(job.id);
    } catch {
      return true; // keep malformed lines
    }
  });

  fs.writeFileSync(JOBS_FILE, remaining.join('\n') + '\n', 'utf8');
  console.log(`🗑️  Removed ${closed} closed jobs (${lines.length} → ${remaining.length})`);
}

main().catch(err => {
  console.error('❌ Fatal:', err.message);
  process.exit(1);
});
