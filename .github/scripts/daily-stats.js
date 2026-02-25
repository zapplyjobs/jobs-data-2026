#!/usr/bin/env node

/**
 * Daily GitHub Stats to Discord
 *
 * Posts daily org stats to #github-updates channel:
 * - Section 1: Repository stars (with deltas from previous day)
 * - Section 2: Workflow health (today's runs)
 * - Section 3: Job pipeline stats (jobs per repo)
 *
 * Persists previous day's star counts in .github/data/daily-stats.json
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const { Client, GatewayIntentBits } = require('discord.js');

const STATS_FILE = path.join(process.cwd(), '.github', 'data', 'daily-stats.json');
const ALL_JOBS_FILE = path.join(process.cwd(), '.github', 'data', 'all_jobs.json');

const ORG = 'zapplyjobs';
const CHANNEL_ID = process.env.DISCORD_DAILY_STATS_CHANNEL_ID;

// Repos to track (in display order)
const TRACKED_REPOS = [
  'New-Grad-Jobs-2026',
  'Internships-2026',
  'New-Grad-Software-Engineering-Jobs-2026',
  'New-Grad-Data-Science-Jobs-2026',
  'New-Grad-Hardware-Engineering-Jobs-2026',
  'New-Grad-Nursing-Jobs-2026',
];

// Consumer repos with current_jobs.json
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
  if (previous == null) return '';
  const diff = current - previous;
  if (diff === 0) return '(=)';
  return diff > 0 ? `(+${diff})` : `(${diff})`;
}

async function main() {
  if (!CHANNEL_ID) { console.error('DISCORD_DAILY_STATS_CHANNEL_ID not set'); process.exit(1); }

  // Load previous stats
  let prevStats = {};
  if (fs.existsSync(STATS_FILE)) {
    try { prevStats = JSON.parse(fs.readFileSync(STATS_FILE, 'utf8')); } catch {}
  }

  // --- Section 1: Stars ---
  const allRepos = await githubGet(`/orgs/${ORG}/repos?type=public&per_page=100`);
  const repoMap = {};
  for (const r of allRepos) repoMap[r.name] = r;

  const today = new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  let starsLines = '';
  let totalStars = 0;
  let totalDelta = 0;

  for (const name of TRACKED_REPOS) {
    const r = repoMap[name];
    if (!r) continue;
    const stars = r.stargazers_count;
    const prev = prevStats.stars?.[name];
    const d = prev != null ? stars - prev : null;
    if (d != null) totalDelta += d;
    totalStars += stars;
    const label = name.padEnd(42);
    starsLines += `${label} ${stars} ${delta(stars, prev)}\n`;
  }

  const deltaStr = totalDelta === 0 ? 'no change' : (totalDelta > 0 ? `+${totalDelta} today` : `${totalDelta} today`);

  // --- Section 2: Workflow Health (today) ---
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);

  let workflowLines = '';
  try {
    const runs = await githubGet(`/repos/${ORG}/jobs-data-2026/actions/runs?per_page=100&created=>=${todayStart.toISOString()}`);
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

  // --- Section 3: Job Pipeline ---
  let pipelineLines = '';

  // all_jobs.json local
  try {
    const lines = fs.readFileSync(ALL_JOBS_FILE, 'utf8').split('\n').filter(l => l.trim());
    pipelineLines += `${'jobs-data-2026 / all_jobs.json'.padEnd(42)} ${lines.length}\n`;
  } catch {
    pipelineLines += `${'jobs-data-2026 / all_jobs.json'.padEnd(42)} (unavailable)\n`;
  }

  // Consumer repos via raw URL
  for (const { repo, label } of CONSUMER_REPOS) {
    const raw = await rawGet(`https://raw.githubusercontent.com/${ORG}/${repo}/main/.github/data/current_jobs.json`);
    let count = '?';
    if (raw) {
      try { count = JSON.parse(raw).length; } catch {}
    }
    pipelineLines += `${label.padEnd(42)} ${count}\n`;
  }

  // Discord posted last 24h
  try {
    const postedFile = path.join(process.cwd(), '.github', 'data', 'posted_jobs.json');
    const posted = JSON.parse(fs.readFileSync(postedFile, 'utf8'));
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    const recentCount = Object.values(posted).filter(entry => {
      const ts = entry.postedAt || entry.timestamp || entry.lastSeen;
      return ts && new Date(ts).getTime() > cutoff;
    }).length;
    pipelineLines += `${'Discord posted (last 24h)'.padEnd(42)} ${recentCount}\n`;
  } catch {
    pipelineLines += `${'Discord posted (last 24h)'.padEnd(42)} ?\n`;
  }

  // --- Build message ---
  const msg1 = `📊 zapplyjobs Org — ${today} — Daily\n\n⭐ STARS\n\`\`\`\n${starsLines}\nTotal: ${totalStars} stars (${deltaStr})\n\`\`\``;
  const msg2 = `🤖 WORKFLOW HEALTH\n\`\`\`\n${workflowLines || '(no runs today)\n'}\`\`\``;
  const msg3 = `📋 JOB PIPELINE\n\`\`\`\n${pipelineLines}\`\`\``;

  // --- Post to Discord ---
  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  await client.login(process.env.DISCORD_TOKEN);
  await new Promise(r => client.once('ready', r));

  const channel = await client.channels.fetch(CHANNEL_ID, { allowUnknownGuild: true });
  if (!channel || !channel.isTextBased()) throw new Error(`Channel ${CHANNEL_ID} not found or not a text channel`);
  await channel.send(msg1);
  await channel.send(msg2);
  await channel.send(msg3);

  console.log('✅ Daily stats posted to Discord');
  await client.destroy();

  // --- Persist today's star counts ---
  const newStats = { date: new Date().toISOString(), stars: {} };
  for (const name of TRACKED_REPOS) {
    if (repoMap[name]) newStats.stars[name] = repoMap[name].stargazers_count;
  }
  fs.writeFileSync(STATS_FILE, JSON.stringify(newStats, null, 2), 'utf8');
  console.log('✅ Saved daily-stats.json');
}

main().catch(err => {
  console.error('❌ Fatal error:', err.message);
  process.exit(1);
});
