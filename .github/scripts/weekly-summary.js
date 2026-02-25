#!/usr/bin/env node

/**
 * Weekly GitHub Summary to Discord
 *
 * Posts weekly org summary to #github-updates channel:
 * - Repository stats table (stars, forks, issues with weekly deltas)
 * - Workflow health for last 7 days
 * - Job pipeline weekly averages
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

const CONSUMER_REPOS = [
  { repo: 'New-Grad-Jobs-2026', label: 'New-Grad-Jobs-2026' },
  { repo: 'Internships-2026', label: 'Internships-2026' },
  { repo: 'New-Grad-Software-Engineering-Jobs-2026', label: 'Software-Engineering' },
  { repo: 'New-Grad-Data-Science-Jobs-2026', label: 'Data-Science' },
  { repo: 'New-Grad-Hardware-Engineering-Jobs-2026', label: 'Hardware-Engineering' },
  { repo: 'New-Grad-Nursing-Jobs-2026', label: 'Nursing' },
];

function githubGet(path) {
  return new Promise((resolve, reject) => {
    https.get(`https://api.github.com${path}`, {
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

  // --- Section 2: Workflow health (last 7 days) ---
  const weekAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);
  let workflowLines = '';
  try {
    const runs = await githubGet(`/repos/${ORG}/jobs-data-2026/actions/runs?per_page=100&created=>=${weekAgo.toISOString()}`);
    const byWorkflow = {};
    for (const run of (runs.workflow_runs || [])) {
      const name = run.name || run.path;
      if (!byWorkflow[name]) byWorkflow[name] = { runs: 0, success: 0, fail: 0, durations: [] };
      byWorkflow[name].runs++;
      if (run.conclusion === 'success') byWorkflow[name].success++;
      if (run.conclusion === 'failure') byWorkflow[name].fail++;
      if (run.run_started_at && run.updated_at) {
        const dur = Math.round((new Date(run.updated_at) - new Date(run.run_started_at)) / 1000);
        byWorkflow[name].durations.push(dur);
      }
    }
    for (const [name, w] of Object.entries(byWorkflow)) {
      const avgDur = w.durations.length ? Math.round(w.durations.reduce((a, b) => a + b, 0) / w.durations.length) : 0;
      const failPct = w.runs ? ((w.fail / w.runs) * 100).toFixed(1) : '0.0';
      const warn = w.fail > 0 ? ' ⚠️' : '';
      const label = name.slice(0, 35).padEnd(35);
      workflowLines += `${label} | ${w.runs} runs | ${w.success}✅ ${w.fail}❌ | ~${avgDur}s | ${failPct}% fail${warn}\n`;
    }
  } catch (e) {
    workflowLines = `(workflow data unavailable: ${e.message})\n`;
  }

  // --- Section 3: Pipeline snapshot ---
  let pipelineLines = '';
  try {
    const lines = fs.readFileSync(ALL_JOBS_FILE, 'utf8').split('\n').filter(l => l.trim());
    pipelineLines += `${'all_jobs.json (pipeline)'.padEnd(42)} ${lines.length}\n`;
  } catch {
    pipelineLines += `${'all_jobs.json (pipeline)'.padEnd(42)} (unavailable)\n`;
  }

  for (const { repo, label } of CONSUMER_REPOS) {
    const raw = await rawGet(`https://raw.githubusercontent.com/${ORG}/${repo}/main/.github/data/current_jobs.json`);
    let count = '?';
    if (raw) {
      try { count = JSON.parse(raw).length; } catch {}
    }
    pipelineLines += `${label.padEnd(42)} ${count}\n`;
  }

  // --- Build messages ---
  const msg1 = `📊 zapplyjobs Org — Weekly Summary\nWeek of ${weekRange()}\n\n━━━ REPOSITORY STATS ━━━\n\`\`\`\n${header}\n${divider}\n${repoLines}\`\`\`${totalsLine}`;
  const msg2 = `━━━ WORKFLOW HEALTH (Last 7 Days) ━━━\n\`\`\`\n${workflowLines || '(no runs this week)\n'}\`\`\``;
  const msg3 = `━━━ JOB PIPELINE (Current Snapshot) ━━━\n\`\`\`\n${pipelineLines}\`\`\``;

  // --- Post to Discord ---
  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  await client.login(process.env.DISCORD_TOKEN);
  await new Promise(r => client.once('ready', r));

  const channel = await client.channels.fetch(CHANNEL_ID, { allowUnknownGuild: true });
  if (!channel || !channel.isTextBased()) throw new Error(`Channel ${CHANNEL_ID} not found or not a text channel`);
  await channel.send(msg1);
  await channel.send(msg2);
  await channel.send(msg3);

  console.log('✅ Weekly summary posted to Discord');
  await client.destroy();

  // --- Persist this week's stats ---
  const newStats = { date: new Date().toISOString(), stars: {}, forks: {} };
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
