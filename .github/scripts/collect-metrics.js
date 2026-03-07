#!/usr/bin/env node

/**
 * Collect Metrics for Aggregator Monitoring
 *
 * Pipeline-level data is read locally from jobs-metadata.json + all_jobs.json.
 * Per-repo data is fetched from GitHub API (workflow status filtered to update-jobs.yml,
 * last jobs update from commits API filtered to the data file path).
 *
 * Output: overwrites .github/data/metrics/latest.json, appends to history.jsonl.
 * Cron: every 6 hours (collect-metrics.yml).
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const METRICS_DIR = path.join(DATA_DIR, 'metrics');
const LATEST_FILE = path.join(METRICS_DIR, 'latest.json');
const HISTORY_FILE = path.join(METRICS_DIR, 'history.jsonl');

// Repos to monitor. New-Grad has no current_jobs.json by design — jobCount will be null.
const REPOS = [
  { owner: 'zapplyjobs', repo: 'New-Grad-Jobs-2026',                      name: 'New-Grad',      hasJobsFile: false },
  { owner: 'zapplyjobs', repo: 'Internships-2026',                         name: 'Internships',   hasJobsFile: true  },
  { owner: 'zapplyjobs', repo: 'New-Grad-Software-Engineering-Jobs-2026',  name: 'Software',      hasJobsFile: true  },
  { owner: 'zapplyjobs', repo: 'New-Grad-Data-Science-Jobs-2026',          name: 'Data-Science',  hasJobsFile: true  },
  { owner: 'zapplyjobs', repo: 'New-Grad-Hardware-Engineering-Jobs-2026',  name: 'Hardware',      hasJobsFile: true  },
  { owner: 'zapplyjobs', repo: 'New-Grad-Nursing-Jobs-2026',               name: 'Nursing',       hasJobsFile: true  },
];

function ghRequest(url) {
  return new Promise((resolve, reject) => {
    https.get(url, {
      headers: {
        'User-Agent': 'Zapply-Metrics-Bot',
        'Authorization': `Bearer ${process.env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github.v3+json'
      }
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(data) }); }
        catch { resolve({ status: res.statusCode, body: null }); }
      });
    }).on('error', reject);
  });
}

function rawRequest(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { headers: { 'User-Agent': 'Zapply-Metrics-Bot' } }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve({ status: res.statusCode, body: data }));
    }).on('error', reject);
  });
}

/**
 * Get job count from current_jobs.json via raw GitHub URL.
 * Returns null for repos that don't write this file (New-Grad).
 */
async function getJobCount(owner, repo) {
  const url = `https://raw.githubusercontent.com/${owner}/${repo}/main/.github/data/current_jobs.json`;
  try {
    const res = await rawRequest(url);
    if (res.status !== 200) return null;
    const jobs = JSON.parse(res.body);
    return Array.isArray(jobs) ? jobs.length : null;
  } catch {
    return null;
  }
}

/**
 * Get the timestamp of the last commit that touched a specific file path.
 * Uses commits API with path filter — accurate even if other commits landed after.
 */
async function getLastFileCommitTimestamp(owner, repo, filePath) {
  const url = `https://api.github.com/repos/${owner}/${repo}/commits?path=${filePath}&per_page=1`;
  try {
    const res = await ghRequest(url);
    if (res.status !== 200 || !Array.isArray(res.body) || res.body.length === 0) return null;
    return res.body[0].commit?.committer?.date || res.body[0].commit?.author?.date || null;
  } catch {
    return null;
  }
}

/**
 * Get status of the last run of update-jobs.yml specifically.
 * Avoids the bug in the old script which picked up any workflow run (pages, cleanup, etc.).
 */
async function getUpdateJobsWorkflowStatus(owner, repo) {
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/update-jobs.yml/runs?per_page=1`;
  try {
    const res = await ghRequest(url);
    if (res.status !== 200 || !res.body?.workflow_runs?.length) return null;
    const run = res.body.workflow_runs[0];
    return {
      status: run.status,
      conclusion: run.conclusion,
      lastRun: run.updated_at
    };
  } catch {
    return null;
  }
}

async function getRepoMetrics(repo) {
  const [jobCount, lastJobsUpdate, workflowData] = await Promise.all([
    repo.hasJobsFile ? getJobCount(repo.owner, repo.repo) : Promise.resolve(null),
    repo.hasJobsFile
      ? getLastFileCommitTimestamp(repo.owner, repo.repo, '.github/data/current_jobs.json')
      : Promise.resolve(null),
    getUpdateJobsWorkflowStatus(repo.owner, repo.repo)
  ]);

  return {
    name: repo.name,
    // null means "not applicable by design" (New-Grad), not a failure
    jobCount,
    lastJobsUpdate,
    workflowStatus: workflowData?.conclusion || null,
    workflowLastRun: workflowData?.lastRun || null
  };
}

/**
 * Read pipeline-level data from local files (same repo, available at runtime).
 * all_jobs.json is JSONL — line count = pipeline size.
 */
function getPipelineMetrics() {
  try {
    const metadataPath = path.join(DATA_DIR, 'jobs-metadata.json');
    const allJobsPath = path.join(DATA_DIR, 'all_jobs.json');

    if (!fs.existsSync(metadataPath)) {
      console.warn('  ⚠️  jobs-metadata.json not found — pipeline metrics unavailable');
      return null;
    }

    const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));

    // JSONL line count for pipeline total
    let pipelineTotal = null;
    if (fs.existsSync(allJobsPath)) {
      const content = fs.readFileSync(allJobsPath, 'utf8');
      pipelineTotal = content.split('\n').filter(l => l.trim()).length;
    }

    return {
      pipelineTotal,
      prevTotalJobs: metadata.total_jobs ?? null,
      bySource: metadata.by_source || null,
      jsearchTotalFetched: metadata.jsearch_stats?.total_fetched ?? null,
      tagStats: {
        usTagged: metadata.tag_stats?.locations?.us ?? null,
        entryLevel: metadata.tag_stats?.employment?.entry_level ?? null,
        internship: metadata.tag_stats?.employment?.internship ?? null,
        domains: metadata.tag_stats?.domains || null
      },
      duplicatesRemoved: metadata.duplicates_removed ?? null,
      generatedAt: metadata.generated || null
    };
  } catch (err) {
    console.warn('  ⚠️  Error reading pipeline metrics:', err.message);
    return null;
  }
}

/**
 * Compute week-over-week growth trends from history.jsonl.
 * Finds the snapshot closest to 7 days ago and diffs against current pipeline metrics.
 * Returns null if history is too short (<7 days).
 */
function computeGrowthTrends(currentPipeline) {
  if (!fs.existsSync(HISTORY_FILE) || !currentPipeline) return null;

  const lines = fs.readFileSync(HISTORY_FILE, 'utf8').trim().split('\n').filter(Boolean);
  if (lines.length < 2) return null;

  const now = Date.now();
  const TARGET_AGE_MS = 7 * 24 * 60 * 60 * 1000;

  // Find snapshot closest to 7 days ago
  let best = null;
  let bestDiff = Infinity;
  for (const line of lines) {
    try {
      const snap = JSON.parse(line);
      const age = now - new Date(snap.timestamp).getTime();
      const diff = Math.abs(age - TARGET_AGE_MS);
      if (diff < bestDiff) { bestDiff = diff; best = snap; }
    } catch { /* skip malformed */ }
  }

  if (!best || bestDiff > TARGET_AGE_MS) return null; // no snapshot within 7 days window

  const prevTotal = best.pipeline?.pipelineTotal ?? null;
  const currTotal = currentPipeline.pipelineTotal ?? null;
  const prevBySource = best.pipeline?.bySource ?? {};
  const currBySource = currentPipeline.bySource ?? {};

  const totalDelta = (currTotal !== null && prevTotal !== null) ? currTotal - prevTotal : null;
  const bySourceDelta = {};
  for (const src of new Set([...Object.keys(prevBySource), ...Object.keys(currBySource)])) {
    const prev = prevBySource[src] ?? 0;
    const curr = currBySource[src] ?? 0;
    bySourceDelta[src] = curr - prev;
  }

  return {
    compared_to: best.timestamp,
    total_delta: totalDelta,
    total_delta_pct: (prevTotal && totalDelta !== null) ? Math.round((totalDelta / prevTotal) * 100) : null,
    by_source_delta: bySourceDelta,
  };
}

async function main() {
  console.log('🔍 Collecting metrics...');

  if (!fs.existsSync(METRICS_DIR)) fs.mkdirSync(METRICS_DIR, { recursive: true });

  const pipeline = getPipelineMetrics();
  console.log(`  Pipeline: ${pipeline?.pipelineTotal ?? 'n/a'} jobs total`);

  console.log('  Fetching per-repo data...');
  const repoResults = await Promise.all(REPOS.map(getRepoMetrics));

  const repos = {};
  let operationalCount = 0, failedCount = 0;
  for (const r of repoResults) {
    repos[r.name] = r;
    if (r.workflowStatus === 'success') operationalCount++;
    if (r.workflowStatus === 'failure') failedCount++;
    const jobStr = r.jobCount !== null ? `${r.jobCount} jobs` : 'n/a (by design)';
    const wfStr = r.workflowStatus || 'unknown';
    const emoji = r.workflowStatus === 'success' ? '✅' : r.workflowStatus === 'failure' ? '❌' : '⚠️';
    console.log(`  ${emoji} ${r.name}: ${jobStr}, workflow=${wfStr}`);
  }

  const growth = computeGrowthTrends(pipeline);
  if (growth) {
    const sign = growth.total_delta >= 0 ? '+' : '';
    console.log(`  Week-over-week: ${sign}${growth.total_delta} jobs (${sign}${growth.total_delta_pct}%) vs ${growth.compared_to}`);
  }

  const snapshot = {
    timestamp: new Date().toISOString(),
    pipeline,
    repos,
    summary: { operationalRepos: operationalCount, failedRepos: failedCount },
    growth,
  };

  fs.writeFileSync(LATEST_FILE, JSON.stringify(snapshot, null, 2) + '\n');
  fs.appendFileSync(HISTORY_FILE, JSON.stringify(snapshot) + '\n');

  console.log(`\n✅ latest.json written, history.jsonl appended`);
  if (failedCount > 0) console.log(`⚠️  ${failedCount} repo(s) reporting workflow failure`);
}

main().catch(err => {
  console.error('❌ Fatal:', err.message);
  process.exit(1);
});
