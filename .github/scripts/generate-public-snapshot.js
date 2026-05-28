#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const https = require('https');

const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const SNAPSHOT_PATH = path.join(DATA_DIR, 'zjp-public-snapshot.json');
const STALE_HOURS = 2;

const CONSUMER_REPOS = [
  { owner: 'zapplyjobs', repo: 'New-Grad-Jobs-2026',                    name: 'New-Grad' },
  { owner: 'zapplyjobs', repo: 'Internships-2026',                      name: 'Internships' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Software-Engineering-Jobs-2026', name: 'Software' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Data-Science-Jobs-2026',       name: 'Data-Science' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Hardware-Engineering-Jobs-2026', name: 'Hardware' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Healthcare-Jobs-2026',         name: 'Healthcare' },
  { owner: 'zapplyjobs', repo: 'jobs-aggregator-private',               name: 'Aggregator' },
];

function ghRequest(url) {
  return new Promise((resolve, reject) => {
    https.get(url, {
      headers: {
        'User-Agent': 'ZJP-Public-Snapshot-Bot',
        'Authorization': `Bearer ${process.env.GH_PAT || process.env.GITHUB_TOKEN}`,
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

async function getStars(owner, repo) {
  try {
    const res = await ghRequest(`https://api.github.com/repos/${owner}/${repo}`);
    return res.status === 200 ? (res.body?.stargazers_count ?? null) : null;
  } catch { return null; }
}

async function getSubmoduleHash(owner, repo) {
  try {
    const res = await ghRequest(`https://api.github.com/repos/${owner}/${repo}/contents/.github/scripts/shared`);
    if (res.status === 200 && res.body?.sha) return res.body.sha.slice(0, 7);
    return null;
  } catch { return null; }
}

async function getLastWorkflowStatus(owner, repo) {
  try {
    const res = await ghRequest(`https://api.github.com/repos/${owner}/${repo}/actions/workflows/update-jobs.yml/runs?per_page=1`);
    if (res.status !== 200 || !res.body?.workflow_runs?.length) return null;
    return res.body.workflow_runs[0].conclusion || null;
  } catch { return null; }
}

function readJson(name) {
  const p = path.join(DATA_DIR, name);
  if (!fs.existsSync(p)) return null;
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); }
  catch { return null; }
}

function readPreviousSnapshot() {
  return readJson('zjp-public-snapshot.json');
}

const TECH_DOMAINS = new Set(['software', 'data_science', 'hardware', 'ai']);

function computeG1Metric() {
  const p = path.join(DATA_DIR, 'all_jobs.json');
  if (!fs.existsSync(p)) return null;
  try {
    const lines = fs.readFileSync(p, 'utf8').split('\n').filter(l => l.trim());
    let usTotal = 0, usGeneral = 0, usTech = 0;
    for (const line of lines) {
      try {
        const job = JSON.parse(line);
        const locations = job.tags?.locations || [];
        if (!locations.includes('us')) continue;
        if (job.tags?.employment === 'senior') continue;
        const domains = job.tags?.domains || [];
        const isGeneral = domains.length === 1 && domains[0] === 'general';
        const hasTech = domains.some(d => TECH_DOMAINS.has(d));
        usTotal++;
        if (isGeneral) usGeneral++;
        if (hasTech) usTech++;
      } catch {}
    }
    if (usTotal === 0) return null;
    const techPool = usTech + usGeneral;
    return {
      us_total: usTotal,
      us_general: usGeneral,
      us_general_rate: Math.round((usGeneral / usTotal) * 1000) / 10,
      tech_us_total: usTech,
      tech_us_general_rate: techPool > 0 ? Math.round((usGeneral / techPool) * 1000) / 10 : null,
    };
  } catch { return null; }
}

function computeDeltas(currentPool, previousSnapshot) {
  if (!previousSnapshot?.pool) return null;
  const prev = previousSnapshot.pool;
  const totalDelta = currentPool.total !== null && prev.total !== null ? currentPool.total - prev.total : null;
  const prevTs = previousSnapshot.meta?.generated_at || null;
  return { compared_to: prevTs, total_delta: totalDelta };
}

async function getRepoMetrics(repo) {
  const [stars, workflowStatus, submodule] = await Promise.all([
    getStars(repo.owner, repo.repo),
    repo.name === 'Aggregator' ? Promise.resolve('success') : getLastWorkflowStatus(repo.owner, repo.repo),
    getSubmoduleHash(repo.owner, repo.repo),
  ]);
  return { name: repo.name, stars, workflowStatus, submodule };
}

async function buildSnapshot() {
  const metadata = readJson('jobs-metadata.json');
  const enrichStats = readJson('enrichment-stats.json');
  const previousSnapshot = readPreviousSnapshot();
  const g1 = computeG1Metric();

  const repoMetrics = await Promise.all(CONSUMER_REPOS.map(getRepoMetrics));
  const repos = Object.fromEntries(repoMetrics.map(r => [r.name, {
    stars: r.stars,
    workflowStatus: r.workflowStatus,
    submodule: r.submodule,
  }]));
  const aggregatorSubmodule = repoMetrics.find(r => r.name === 'Aggregator')?.submodule || null;

  const pool = {
    total: metadata?.total_jobs ?? null,
    by_source: metadata?.by_source ? Object.fromEntries(Object.entries(metadata.by_source).map(([k, v]) => [k, v.total ?? v])) : null,
    by_domain: metadata?.tag_stats?.domains || null,
    us_entry_level: metadata?.tag_stats?.locations?.us ?? null,
    us_interns: metadata?.tag_stats?.employment?.internship ?? null,
    g1_metric: g1,
    ats_stats: metadata?.ats_stats || null,
  };

  const tagHistoryEntry = g1 ? {
    date: metadata?.generated || new Date().toISOString(),
    g1_rate: g1.us_general_rate,
    us_general: g1.us_general,
    us_total: g1.us_total,
    senior: metadata?.tag_stats?.employment?.senior ?? null,
    mid_level: metadata?.tag_stats?.employment?.mid_level ?? null,
    entry_level: metadata?.tag_stats?.employment?.entry_level ?? null,
    internship: metadata?.tag_stats?.employment?.internship ?? null,
  } : null;
  const prevTagHistory = previousSnapshot?.tag_history || [];
  const thirtyDaysAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;
  const tagHistory = [
    ...prevTagHistory.filter(e => new Date(e.date).getTime() > thirtyDaysAgo),
    ...(tagHistoryEntry ? [tagHistoryEntry] : []),
  ];

  const snapshot = {
    meta: {
      generated_at: metadata?.generated || new Date().toISOString(),
      generated_by: 'zjp-public-snapshot v1',
      stale_if_older_than_hours: STALE_HOURS,
    },
    pool,
    pipeline: {
      submodule_head: aggregatorSubmodule,
      last_run_status: 'success',
      last_run_at: metadata?.generated || null,
      supplemental_lanes: {
        oracle: metadata?.supplemental_inputs?.oracle ? {
          generated_at: metadata.supplemental_inputs.oracle.generated_at ?? null,
          jobs_fetched: metadata.supplemental_inputs.oracle.jobs_loaded ?? null,
          duration_ms: null,
        } : null,
        custom: metadata?.supplemental_inputs?.custom ? {
          generated_at: metadata.supplemental_inputs.custom.generated_at ?? null,
          jobs_fetched: metadata.supplemental_inputs.custom.jobs_loaded ?? null,
          duration_ms: null,
          sources: metadata.supplemental_inputs.custom.by_source ?? null,
        } : null,
      },
    },
    repos,
    tag_history: tagHistory,
    deltas: computeDeltas(pool, previousSnapshot),
  };

  if (enrichStats) snapshot.enrichment = {
    total_enriched: enrichStats.total_enriched ?? null,
    total_has_description: enrichStats.total_has_description ?? null,
    workday_waiting_for_desc: enrichStats.workday_waiting_for_desc ?? null,
    jsearch_sidecar_lines: null,
  };

  return snapshot;
}

async function main() {
  const snapshot = await buildSnapshot();
  fs.writeFileSync(SNAPSHOT_PATH, JSON.stringify(snapshot, null, 2) + '\n');
  console.log('[generate-public-snapshot] zjp-public-snapshot.json written');
}

main().catch(err => {
  console.error('[generate-public-snapshot] Fatal:', err.message);
  process.exit(1);
});
