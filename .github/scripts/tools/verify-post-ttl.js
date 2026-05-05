#!/usr/bin/env node

/**
 * Post-TTL Change Verification (S234)
 *
 * Run manually after the 7-day TTL change has propagated (1-2 pipeline cycles).
 * Checks all S234 AGG/SUP deploys against expected targets.
 *
 * Usage: node .github/scripts/tools/verify-post-ttl.js
 */

const fs = require('fs');
const path = require('path');

const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const RESULTS_FILE = path.join(__dirname, 'verify-post-ttl-results.json');

function check(name, value, target, comparator) {
  let status;
  if (comparator === 'lt') status = value < target ? 'green' : 'red';
  else if (comparator === 'gt') status = value > target ? 'green' : 'red';
  else if (comparator === 'eq') status = value === target ? 'green' : 'red';
  else if (comparator === 'range') status = value >= target[0] && value <= target[1] ? 'green' : 'red';
  else status = 'unknown';

  const icon = status === 'green' ? '✅' : status === 'red' ? '❌' : '⚠️';
  const targetStr = Array.isArray(target) ? `${target[0]}-${target[1]}` : target;
  console.log(`${icon} ${name}: ${value} (target: ${comparator} ${targetStr})`);
  return { name, value, target: targetStr, comparator, status };
}

function main() {
  const results = [];
  const now = Date.now();
  const DAY = 86400000;

  // Load pool
  const poolPath = path.join(DATA_DIR, 'all_jobs.json');
  const poolLines = fs.readFileSync(poolPath, 'utf8').trim().split('\n');

  // 1. Pool size
  results.push(check('Pool size', poolLines.length, [25000, 45000], 'range'));

  // 2. Stale >7d in pool
  let stale7d = 0;
  const dupIds = new Map();
  for (const l of poolLines) {
    const j = JSON.parse(l);
    if (j.posted_at && (now - new Date(j.posted_at).getTime()) > 7 * DAY) stale7d++;
    dupIds.set(j.id, (dupIds.get(j.id) || 0) + 1);
  }
  const stalePct = Math.round(stale7d / poolLines.length * 100);
  results.push(check('Pool stale >7d', stalePct, 5, 'lt'));

  // 3. Duplicate IDs
  const dupes = [...dupIds.values()].filter(v => v > 1).length;
  results.push(check('Duplicate IDs in pool', dupes, 10, 'lt'));

  // 4. Consumer output
  const consumerPath = '/mnt/c/Users/Mahd/Videos/Work/Business/Job_Listings/New-Grad-Jobs-2026/.github/data/current_jobs.json';
  if (fs.existsSync(consumerPath)) {
    const consumer = JSON.parse(fs.readFileSync(consumerPath, 'utf8'));
    const jobs = Array.isArray(consumer) ? consumer : [];
    results.push(check('Consumer job count', jobs.length, [10000, 25000], 'range'));

    const consumerIds = new Set();
    let consumerDupes = 0;
    let consumerStale = 0;
    for (const j of jobs) {
      if (consumerIds.has(j.job_id)) consumerDupes++;
      consumerIds.add(j.job_id);
      if (j.job_posted_at_datetime_utc && (now - new Date(j.job_posted_at_datetime_utc).getTime()) > 7 * DAY) consumerStale++;
    }
    results.push(check('Consumer duplicates', consumerDupes, 5, 'lt'));
    results.push(check('Consumer stale >7d', Math.round(consumerStale / jobs.length * 100), 10, 'lt'));
  }

  // 5. JSearch sidecar coverage
  const jsearchSidecar = path.join(DATA_DIR, 'descriptions-jsearch.jsonl');
  if (fs.existsSync(jsearchSidecar)) {
    const sidecarIds = new Set();
    for (const l of fs.readFileSync(jsearchSidecar, 'utf8').trim().split('\n')) {
      try { sidecarIds.add(JSON.parse(l).id); } catch {}
    }
    let jsearchPool = 0;
    for (const l of poolLines) {
      if (JSON.parse(l).source === 'jsearch') jsearchPool++;
    }
    const coveragePct = jsearchPool > 0 ? Math.round(sidecarIds.size / jsearchPool * 100) : 0;
    results.push(check('JSearch sidecar coverage', coveragePct, 40, 'gt'));
  }

  // 6. Senior filter rate
  const metaPath = path.join(DATA_DIR, 'jobs-metadata.json');
  if (fs.existsSync(metaPath)) {
    const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
    const sr = meta.senior_filter_stats;
    if (sr) {
      const rate = Math.round(sr.senior_jobs / (sr.entry_level_jobs + sr.senior_jobs) * 100);
      results.push(check('Senior filter rate', rate, [40, 65], 'range'));
    }
  }

  // 6b. WD >7d in consumer (Q4: do WD evergreen jobs reach users after TTL change?)
  if (fs.existsSync(consumerPath)) {
    const consumerJobs = JSON.parse(fs.readFileSync(consumerPath, 'utf8'));
    const cJobs = Array.isArray(consumerJobs) ? consumerJobs : [];
    let wdStale = 0, wdTotal = 0;
    for (const j of cJobs) {
      if ((j._original_source || j._source || '').includes('workday') || (j.job_apply_link || '').includes('myworkdayjobs')) {
        wdTotal++;
        if (j.job_posted_at_datetime_utc && (now - new Date(j.job_posted_at_datetime_utc).getTime()) > 7 * DAY) wdStale++;
      }
    }
    if (wdTotal > 0) results.push(check('WD >7d in consumer', wdStale, 50, 'lt'));
  }

  // 7. AGG-14 archive
  const archiveDir = path.join(DATA_DIR, 'archive');
  const closedArchives = fs.existsSync(archiveDir)
    ? fs.readdirSync(archiveDir).filter(f => f.startsWith('closed-'))
    : [];
  results.push(check('AGG-14 closed-job archives', closedArchives.length, 0, 'gt'));

  // 8. Pipeline alerts
  const alertPath = path.join(DATA_DIR, 'pipeline-alert.json');
  if (fs.existsSync(alertPath)) {
    const alert = JSON.parse(fs.readFileSync(alertPath, 'utf8'));
    results.push(check('Pipeline alerts', alert.active ? alert.failures.length + ' failures' : 'clear', 'clear', 'eq'));
  }

  // Summary
  const green = results.filter(r => r.status === 'green').length;
  const red = results.filter(r => r.status === 'red').length;
  console.log(`\n${'═'.repeat(50)}`);
  console.log(`Summary: ${green} green, ${red} red out of ${results.length} checks`);

  // Write results
  const output = { checked_at: new Date().toISOString(), results, summary: { green, red, total: results.length } };
  fs.writeFileSync(RESULTS_FILE, JSON.stringify(output, null, 2), 'utf8');
  console.log(`Results written to ${RESULTS_FILE}`);
}

main();
