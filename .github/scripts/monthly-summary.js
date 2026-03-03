#!/usr/bin/env node

/**
 * Monthly GitHub Summary to Discord
 *
 * Posts on the 1st of each month to #github-updates channel:
 * - Repository stats (stars/forks with month-over-month deltas)
 * - Workflow health for last 30 days (all 8 pipeline repos)
 * - Job pipeline snapshot with by-source breakdown + consumer counts + Discord posts
 *
 * Persists previous month's stats in .github/data/monthly-stats.json
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const { Client, GatewayIntentBits } = require('discord.js');

const STATS_FILE = path.join(process.cwd(), '.github', 'data', 'monthly-stats.json');
const ALL_JOBS_FILE = path.join(process.cwd(), '.github', 'data', 'all_jobs.json');
const POSTED_JOBS_FILE = path.join(process.cwd(), '.github', 'data', 'posted_jobs.json');

const ORG = 'zapplyjobs';
const CHANNEL_ID = process.env.DISCORD_WEEKLY_STATS_CHANNEL_ID; // reuse same channel

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
  if (previous == null) return '(first month)';
  const diff = current - previous;
  if (diff === 0) return '(=)';
  return diff > 0 ? `(+${diff})` : `(${diff})`;
}

function fmtNum(n) {
  return n.toLocaleString('en-US');
}

function monthName(date) {
  return date.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
}

async function main() {
  if (!CHANNEL_ID) { console.error('DISCORD_WEEKLY_STATS_CHANNEL_ID not set'); process.exit(1); }

  const now = new Date();
  const prevMonthDate = new Date(now);
  prevMonthDate.setMonth(prevMonthDate.getMonth() - 1);
  const reportMonth = monthName(prevMonthDate); // "February 2026"

  let prevStats = {};
  if (fs.existsSync(STATS_FILE)) {
    try { prevStats = JSON.parse(fs.readFileSync(STATS_FILE, 'utf8')); } catch {}
  }

  // --- Section 1: Repo stats ---
  const allRepos = await githubGet(`/orgs/${ORG}/repos?type=public&per_page=100`);
  const repoMap = {};
  for (const r of allRepos) repoMap[r.name] = r;

  const header = `${'Repo'.padEnd(44)} ⭐Stars    🔀Forks    🐛Issues`;
  const divider = '━'.repeat(72);
  let repoLines = '';
  let totalStars = 0, totalForks = 0, totalStarsDelta = 0;

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
    totalStars += stars; totalForks += forks;
    const label = name.padEnd(44);
    repoLines += `${label} ${String(stars + ' ' + delta(stars, pStars)).padEnd(12)} ${String(forks + ' ' + delta(forks, pForks)).padEnd(12)} ${issues}\n`;
  }

  const totalDeltaStr = totalStarsDelta === 0 ? '(=)' : (totalStarsDelta > 0 ? `+${totalStarsDelta} this month` : `${totalStarsDelta} this month`);

  // --- Section 2: Workflow health (last 30 days, all pipeline repos) ---
  const thirtyDaysAgo = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000);
  const byWorkflow = {};

  await Promise.all(PIPELINE_REPOS.map(async (repo) => {
    try {
      const runs = await githubGet(`/repos/${ORG}/${repo}/actions/runs?per_page=100&created=>=${thirtyDaysAgo.toISOString()}`);
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

  // Pipeline total + by-source breakdown
  if (fs.existsSync(ALL_JOBS_FILE)) {
    try {
      const rawLines = fs.readFileSync(ALL_JOBS_FILE, 'utf8').split('\n').filter(l => l.trim());
      pipelineLines += `${'Pipeline total'.padEnd(28)} ${fmtNum(rawLines.length)}\n`;

      const bySource = {};
      for (const line of rawLines) {
        const src = JSON.parse(line).source;
        if (src) bySource[src] = (bySource[src] || 0) + 1;
      }
      const parts = ['workday','greenhouse','amazon','ashby','lever','jsearch']
        .filter(k => bySource[k])
        .map(k => `${k[0].toUpperCase()}${k.slice(1)}: ${fmtNum(bySource[k])}`);
      if (parts.length) pipelineLines += `${'By source'.padEnd(28)} ${parts.join(' | ')}\n`;
    } catch {
      pipelineLines += `${'Pipeline total'.padEnd(28)} (unavailable)\n`;
    }
  } else {
    pipelineLines += `${'Pipeline total'.padEnd(28)} (unavailable)\n`;
  }

  // Enriched jobs count
  const enrichedPath = path.join(process.cwd(), '.github', 'data', 'enriched_jobs.json');
  if (fs.existsSync(enrichedPath)) {
    const enrichedCount = fs.readFileSync(enrichedPath, 'utf8').split('\n').filter(l => l.trim()).length;
    pipelineLines += `${'Enriched jobs'.padEnd(28)} ${fmtNum(enrichedCount)}\n`;
  }

  // Consumer counts
  pipelineLines += '\n';
  const consumerResults = await Promise.all(CONSUMER_REPOS.map(async ({ repo, label }) => {
    const raw = await rawGet(`https://raw.githubusercontent.com/${ORG}/${repo}/main/.github/data/current_jobs.json?t=${Date.now()}`);
    let count = '?';
    if (raw) { try { count = fmtNum(JSON.parse(raw).length); } catch {} }
    return `${'  ' + label.padEnd(20)} ${count.padStart(6)}`;
  }));
  pipelineLines += `${'Consumer jobs'.padEnd(28)}\n` + consumerResults.join('\n') + '\n';

  // Monthly Discord posts
  try {
    const posted = JSON.parse(fs.readFileSync(POSTED_JOBS_FILE, 'utf8'));
    const monthStart = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    const monthEnd = new Date(now.getFullYear(), now.getMonth(), 1);
    let monthlyPostCount = 0;
    for (const job of (posted.jobs || [])) {
      for (const [, info] of Object.entries(job.discordPosts || {})) {
        const ts = info.postedAt;
        if (!ts) continue;
        const d = new Date(ts);
        if (d >= monthStart && d < monthEnd) monthlyPostCount++;
      }
    }
    pipelineLines += `\n${'Discord posts (prev month)'.padEnd(28)} ${fmtNum(monthlyPostCount)}\n`;
  } catch {
    pipelineLines += `\n${'Discord posts (prev month)'.padEnd(28)} ?\n`;
  }

  // --- Build messages ---
  const msg1 = `📅 zapplyjobs Org — Monthly Summary\n${reportMonth}\n\n━━━ REPOSITORY STATS (Month-over-Month) ━━━\n\`\`\`\n${header}\n${divider}\n${repoLines}\`\`\`\n📊 Org Totals: ${totalStars} stars (${totalDeltaStr}) | ${totalForks} forks`;
  const msg2 = `━━━ WORKFLOW HEALTH (Last 30 Days) ━━━\n\`\`\`\n${workflowLines || '(no runs)\n'}\`\`\``;
  const msg3 = `━━━ JOB PIPELINE ━━━\n\`\`\`\n${pipelineLines}\`\`\``;

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

  console.log('✅ Monthly summary posted to Discord');
  await client.destroy();

  // --- Persist this month's stats ---
  const newStats = { date: now.toISOString(), month: reportMonth, stars: {}, forks: {} };
  for (const name of TRACKED_REPOS) {
    if (repoMap[name]) {
      newStats.stars[name] = repoMap[name].stargazers_count;
      newStats.forks[name] = repoMap[name].forks_count;
    }
  }
  fs.writeFileSync(STATS_FILE, JSON.stringify(newStats, null, 2), 'utf8');
  console.log('✅ Saved monthly-stats.json');
}

main().catch(err => {
  console.error('❌ Fatal error:', err.message);
  process.exit(1);
});
