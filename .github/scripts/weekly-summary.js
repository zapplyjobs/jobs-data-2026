#!/usr/bin/env node

/**
 * Weekly GitHub Summary to Discord
 *
 * Posts weekly org summary to #github-updates channel:
 * - Repository stats table (stars, forks, issues with weekly deltas)
 * - Workflow health for last 7 days (all 8 pipeline repos)
 * - Job pipeline snapshot with by-source breakdown + consumer counts + freshness
 *
 * Persists previous week's stats in .github/data/weekly-stats.json
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const { Client, GatewayIntentBits } = require('discord.js');

const STATS_FILE = path.join(process.cwd(), '.github', 'data', 'weekly-stats.json');
const ALL_JOBS_FILE = path.join(process.cwd(), '.github', 'data', 'all_jobs.json');

const ORG = 'zapplyjobs';
const CHANNEL_ID = process.env.DISCORD_WEEKLY_STATS_CHANNEL_ID;

const TRACKED_REPOS = [
  'New-Grad-Jobs-2026',
  'Internships-2026',
  'New-Grad-Software-Engineering-Jobs-2026',
  'New-Grad-Data-Science-Jobs-2026',
  'New-Grad-Hardware-Engineering-Jobs-2026',
  'New-Grad-Nursing-Jobs-2026',
];

// All repos whose workflow health matters
const PIPELINE_REPOS = [
  'jobs-aggregator-private',
  'jobs-data-2026',
  'New-Grad-Jobs-2026',
  'Internships-2026',
  'New-Grad-Software-Engineering-Jobs-2026',
  'New-Grad-Data-Science-Jobs-2026',
  'New-Grad-Hardware-Engineering-Jobs-2026',
  'New-Grad-Nursing-Jobs-2026',
];

const CONSUMER_REPOS = [
  { repo: 'New-Grad-Jobs-2026',                       label: 'New-Grad' },
  { repo: 'Internships-2026',                         label: 'Internships' },
  { repo: 'New-Grad-Software-Engineering-Jobs-2026',  label: 'Software-Eng' },
  { repo: 'New-Grad-Data-Science-Jobs-2026',          label: 'Data-Science' },
  { repo: 'New-Grad-Hardware-Engineering-Jobs-2026',  label: 'Hardware-Eng' },
  { repo: 'New-Grad-Nursing-Jobs-2026',               label: 'Nursing' },
];

// System workflows to exclude from health display
const SYSTEM_WORKFLOWS = ['pages build and deployment', 'pages-build-deployment', 'CodeQL'];

function githubGet(urlPath) {
  return new Promise((resolve, reject) => {
    https.get(`https://api.github.com${urlPath}`, {
      headers: {
        'User-Agent': 'Zapply-Stats-Bot',
        'Authorization': `Bearer ${process.env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github.v3+json'
      }
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
}

function rawGet(url) {
  return new Promise((resolve) => {
    https.get(url, { headers: { 'User-Agent': 'Zapply-Stats-Bot' } }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(res.statusCode === 200 ? data : null));
    }).on('error', () => resolve(null));
  });
}

function delta(current, previous) {
  if (previous == null) return '(=)';
  const diff = current - previous;
  if (diff === 0) return '(=)';
  return diff > 0 ? `(+${diff})` : `(${diff})`;
}

function fmtNum(n) {
  return n.toLocaleString('en-US');
}

function weekRange() {
  const end = new Date();
  const start = new Date(end);
  start.setDate(start.getDate() - 6);
  const fmt = d => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  return `${fmt(start)} – ${fmt(end)}`;
}

async function main() {
  if (!CHANNEL_ID) { console.error('DISCORD_WEEKLY_STATS_CHANNEL_ID not set'); process.exit(1); }

  let prevStats = {};
  if (fs.existsSync(STATS_FILE)) {
    try { prevStats = JSON.parse(fs.readFileSync(STATS_FILE, 'utf8')); } catch {}
  }

  // --- Fetch all public repos ---
  const allRepos = await githubGet(`/orgs/${ORG}/repos?type=public&per_page=100`);
  const repoMap = {};
  for (const r of allRepos) repoMap[r.name] = r;

  // --- Section 1: Repo stats table ---
  const header = `${'Repo'.padEnd(44)} ⭐Stars    🔀Forks    🐛Issues`;
  const divider = '━'.repeat(70);
  let repoLines = '';
  let totalStars = 0, totalForks = 0, totalIssues = 0;
  let totalStarsDelta = 0;

  for (const name of TRACKED_REPOS) {
    const r = repoMap[name];
    if (!r) continue;
    const stars = r.stargazers_count;
    const forks = r.forks_count;
    const issues = r.open_issues_count;
    const pStars = prevStats.stars?.[name];
    const pForks = prevStats.forks?.[name];
    const d = pStars != null ? stars - pStars : null;
    if (d != null) totalStarsDelta += d;
    totalStars += stars; totalForks += forks; totalIssues += issues;
    const label = name.padEnd(44);
    repoLines += `${label} ${String(stars + ' ' + delta(stars, pStars)).padEnd(12)} ${String(forks + ' ' + delta(forks, pForks)).padEnd(12)} ${issues}\n`;
  }

  const totalDeltaStr = totalStarsDelta === 0 ? '(=)' : (totalStarsDelta > 0 ? `(+${totalStarsDelta})` : `(${totalStarsDelta})`);
  const totalsLine = `\n📊 Org Totals: ${totalStars} stars ${totalDeltaStr} | ${totalForks} forks | ${totalIssues} issues`;

  // --- Section 2: Workflow health (last 7 days, all pipeline repos) ---
  const weekAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);
  const byWorkflow = {};

  await Promise.all(PIPELINE_REPOS.map(async (repo) => {
    try {
      const runs = await githubGet(`/repos/${ORG}/${repo}/actions/runs?per_page=100&created=>=${weekAgo.toISOString()}`);
      for (const run of (runs.workflow_runs || [])) {
        if (SYSTEM_WORKFLOWS.includes(run.name)) continue;
        const key = run.name || run.path;
        if (!byWorkflow[key]) byWorkflow[key] = { runs: 0, success: 0, fail: 0, durations: [] };
        byWorkflow[key].runs++;
        if (run.conclusion === 'success') byWorkflow[key].success++;
        if (run.conclusion === 'failure') byWorkflow[key].fail++;
        if (run.run_started_at && run.updated_at) {
          const dur = Math.round((new Date(run.updated_at) - new Date(run.run_started_at)) / 1000);
          byWorkflow[key].durations.push(dur);
        }
      }
    } catch { /* repo unavailable — skip */ }
  }));

  let workflowLines = '';
  for (const [name, w] of Object.entries(byWorkflow)) {
    const avgDur = w.durations.length ? Math.round(w.durations.reduce((a, b) => a + b, 0) / w.durations.length) : 0;
    const failPct = w.runs ? ((w.fail / w.runs) * 100).toFixed(1) : '0.0';
    const warn = w.fail > 0 ? ' ⚠️' : '';
    const label = name.slice(0, 35).padEnd(35);
    workflowLines += `${label} | ${w.runs} runs | ${w.success}✅ ${w.fail}❌ | ~${avgDur}s | ${failPct}% fail${warn}\n`;
  }

  // --- Section 3: Pipeline snapshot ---
  let pipelineLines = '';

  // Pipeline total with week-over-week delta + anomaly detection
  const prevTotal = prevStats.pipelineTotal ?? null;
  const history = Array.isArray(prevStats.history) ? prevStats.history : [];
  let pipelineTotal = null;
  if (fs.existsSync(ALL_JOBS_FILE)) {
    const rawLines = fs.readFileSync(ALL_JOBS_FILE, 'utf8').split('\n').filter(l => l.trim());
    pipelineTotal = rawLines.length;
    let totalLine = fmtNum(pipelineTotal);
    if (prevTotal != null) {
      const d = pipelineTotal - prevTotal;
      totalLine += d === 0 ? ' (=)' : (d > 0 ? ` (+${d})` : ` (${d})`);
    }
    let anomalyFlag = '';
    if (history.length >= 3) {
      const avg = history.reduce((s, v) => s + v, 0) / history.length;
      if (pipelineTotal < avg * 0.85) anomalyFlag = ' ⚠️ COUNT DROP';
    }
    pipelineLines += `${'Total jobs (ZJP pool)'.padEnd(28)} ${totalLine}${anomalyFlag}\n`;

    // By-source breakdown — count from pool directly (metadata by_source = per-run fetch counts, not pool)
    try {
      const bySource = {};
      for (const line of rawLines) {
        const src = JSON.parse(line).source;
        if (src) bySource[src] = (bySource[src] || 0) + 1;
      }
      const parts = ['workday','greenhouse','amazon','ashby','lever','jsearch']
        .filter(k => bySource[k])
        .map(k => `${k[0].toUpperCase()}${k.slice(1)}: ${fmtNum(bySource[k])}`);
      if (parts.length) pipelineLines += `${'By source'.padEnd(28)} ${parts.join(' | ')}\n`;
    } catch { /* skip */ }
  } else {
    pipelineLines += `${'Total jobs (ZJP pool)'.padEnd(28)} (unavailable)\n`;
  }

  // Enriched jobs count
  const enrichedPath = path.join(process.cwd(), '.github', 'data', 'enriched_jobs.json');
  if (fs.existsSync(enrichedPath)) {
    const enrichedCount = fs.readFileSync(enrichedPath, 'utf8').split('\n').filter(l => l.trim()).length;
    pipelineLines += `${'Enriched jobs'.padEnd(28)} ${fmtNum(enrichedCount)}\n`;
  }

  // Consumer counts + freshness
  pipelineLines += '\n';
  const consumerResults = await Promise.all(CONSUMER_REPOS.map(async ({ repo, label }) => {
    const filePath = '.github/data/current_jobs.json';
    try {
      const [commitData, raw] = await Promise.all([
        githubGet(`/repos/${ORG}/${repo}/commits?path=${encodeURIComponent(filePath)}&per_page=1`),
        rawGet(`https://raw.githubusercontent.com/${ORG}/${repo}/main/${filePath}?t=${Date.now()}`)
      ]);
      const ts = commitData[0]?.commit?.committer?.date || commitData[0]?.commit?.author?.date;
      const ageStr = ts ? (() => {
        const ageMin = Math.round((Date.now() - new Date(ts).getTime()) / 60000);
        return (ageMin < 60 ? `${ageMin}m` : `${Math.round(ageMin / 60)}h`) + ' ago';
      })() : '?';
      const ageFlag = ts && Math.round((Date.now() - new Date(ts).getTime()) / 60000) > 360 ? ' ⚠️' : '';
      let count = '?';
      if (raw) { try { count = fmtNum(JSON.parse(raw).length); } catch {} }
      return `${'  ' + label.padEnd(20)} ${count.padStart(6)}  ${ageStr}${ageFlag}`;
    } catch {
      return `${'  ' + label.padEnd(20)} ${'?'.padStart(6)}  (unavailable)`;
    }
  }));
  pipelineLines += `${'Consumer jobs / freshness'.padEnd(28)}\n` + consumerResults.join('\n') + '\n';

  // Discord posted last 7 days
  try {
    const postedFile = path.join(process.cwd(), '.github', 'data', 'posted_jobs.json');
    const posted = JSON.parse(fs.readFileSync(postedFile, 'utf8'));
    const jobs = Array.isArray(posted.jobs) ? posted.jobs : [];
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    const recentCount = jobs.filter(j => j.postedToDiscord && new Date(j.postedToDiscord).getTime() > cutoff).length;
    pipelineLines += `${'Discord posted (last 7d)'.padEnd(28)} ${recentCount}\n`;
  } catch {
    pipelineLines += `${'Discord posted (last 7d)'.padEnd(28)} ?\n`;
  }

  // --- Build messages ---
  const msg1 = `📊 **zapplyjobs Org — Weekly Summary**\nWeek of ${weekRange()}\n\n⭐ **REPOSITORY STATS**\n\`\`\`\n${header}\n${divider}\n${repoLines}\`\`\`${totalsLine}`;
  const msg2 = `🤖 **WORKFLOW HEALTH (Last 7 Days)**\n\`\`\`\n${workflowLines || '(no runs this week)\n'}\`\`\``;
  const msg3 = `📋 **JOB PIPELINE**\n\`\`\`\n${pipelineLines}\`\`\``;

  // --- Post to Discord ---
  const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages] });
  await client.login(process.env.DISCORD_TOKEN);
  await new Promise(r => client.once('ready', r));

  await Promise.all(client.guilds.cache.map(g => g.channels.fetch()));
  const channel = await client.channels.fetch(CHANNEL_ID);
  if (!channel || !channel.isTextBased()) throw new Error(`Channel ${CHANNEL_ID} not found or not a text channel`);
  await channel.send(msg1);
  await channel.send(msg2);
  await channel.send(msg3);

  console.log('✅ Weekly summary posted to Discord');
  await client.destroy();

  // --- Persist this week's stats ---
  const newHistory = pipelineTotal != null
    ? [...history, pipelineTotal].slice(-4)  // keep last 4 weeks
    : history;
  const newStats = { date: new Date().toISOString(), stars: {}, forks: {}, pipelineTotal, history: newHistory };
  for (const name of TRACKED_REPOS) {
    if (repoMap[name]) {
      newStats.stars[name] = repoMap[name].stargazers_count;
      newStats.forks[name] = repoMap[name].forks_count;
    }
  }
  fs.writeFileSync(STATS_FILE, JSON.stringify(newStats, null, 2), 'utf8');
  console.log('✅ Saved weekly-stats.json');
}

main().catch(err => {
  console.error('❌ Fatal error:', err.message);
  process.exit(1);
});
