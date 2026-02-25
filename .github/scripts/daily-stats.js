#!/usr/bin/env node

/**
 * Daily GitHub Stats to Discord
 *
 * Posts daily org stats to #github-updates channel:
 * - Section 1: Repository stars (all public repos, with deltas)
 * - Section 2: Per-repo workflow health (last 24h, all 9 repos)
 * - Section 3: Job pipeline stats
 *
 * Persists previous day's star counts in .github/data/daily-stats.json
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const { Client, GatewayIntentBits } = require('discord.js');

const STATS_FILE = path.join(process.cwd(), '.github', 'data', 'daily-stats.json');

const ORG = 'zapplyjobs';
const CHANNEL_ID = process.env.DISCORD_DAILY_STATS_CHANNEL_ID;

// All repos to track for stars (in display order — high-star first)
const STAR_REPOS = [
  'Research-Internships-for-Undergraduates',
  'underclassmen-internships',
  'New-Grad-Jobs-2026',
  'Internships-2026',
  'New-Grad-Software-Engineering-Jobs-2026',
  'New-Grad-Data-Science-Jobs-2026',
  'New-Grad-Hardware-Engineering-Jobs-2026',
  'New-Grad-Nursing-Jobs-2026',
  'resume-samples-2026',
  'interview-handbook-2026',
  'Remote-Jobs-2026',
];

// All 9 pipeline repos to check workflow health
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

// Consumer repos with current_jobs.json on GitHub
const CONSUMER_REPOS = [
  { repo: 'New-Grad-Jobs-2026',                      label: 'New-Grad' },
  { repo: 'Internships-2026',                         label: 'Internships' },
  { repo: 'New-Grad-Software-Engineering-Jobs-2026',  label: 'Software-Eng' },
  { repo: 'New-Grad-Data-Science-Jobs-2026',          label: 'Data-Science' },
  { repo: 'New-Grad-Hardware-Engineering-Jobs-2026',  label: 'Hardware-Eng' },
  { repo: 'New-Grad-Nursing-Jobs-2026',               label: 'Nursing' },
];

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
  if (previous == null) return '';
  const diff = current - previous;
  if (diff === 0) return '(=)';
  return diff > 0 ? `(+${diff})` : `(${diff})`;
}

function fmtNum(n) {
  return n.toLocaleString('en-US');
}

async function main() {
  if (!CHANNEL_ID) { console.error('DISCORD_DAILY_STATS_CHANNEL_ID not set'); process.exit(1); }

  // Load previous stats
  let prevStats = {};
  if (fs.existsSync(STATS_FILE)) {
    try { prevStats = JSON.parse(fs.readFileSync(STATS_FILE, 'utf8')); } catch {}
  }

  const today = new Date().toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

  // --- Section 1: Stars (all tracked repos) ---
  const allRepos = await githubGet(`/orgs/${ORG}/repos?type=public&per_page=100`);
  const repoMap = {};
  for (const r of allRepos) repoMap[r.name] = r;

  let starsLines = '';
  let totalStars = 0;
  let totalDelta = 0;

  for (const name of STAR_REPOS) {
    const r = repoMap[name];
    if (!r) continue;
    const stars = r.stargazers_count;
    const prev = prevStats.stars?.[name];
    const d = prev != null ? stars - prev : null;
    if (d != null) totalDelta += d;
    totalStars += stars;
    const label = name.slice(0, 40).padEnd(40);
    starsLines += `${label} ${fmtNum(stars).padStart(6)} ${delta(stars, prev)}\n`;
  }

  const deltaStr = totalDelta === 0 ? 'no change' : (totalDelta > 0 ? `+${totalDelta} today` : `${totalDelta} today`);

  // --- Section 2: Per-repo workflow health (last 24h) ---
  const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  let workflowLines = '';

  await Promise.all(PIPELINE_REPOS.map(async (repo) => {
    try {
      const runs = await githubGet(`/repos/${ORG}/${repo}/actions/runs?per_page=100&created=>=${since}`);
      const list = runs.workflow_runs || [];
      const total = list.length;
      const success = list.filter(r => r.conclusion === 'success').length;
      const fail = list.filter(r => r.conclusion === 'failure').length;
      const inProgress = list.filter(r => r.status === 'in_progress').length;
      const status = fail > 0 ? '⚠️' : (total === 0 ? '➖' : '✅');
      const label = repo.slice(0, 38).padEnd(38);
      workflowLines += `${status} ${label} ${success}✅ ${fail}❌ ${inProgress > 0 ? `${inProgress}🔄` : ''} (${total} runs)\n`;
    } catch {
      workflowLines += `➖ ${repo.slice(0, 38).padEnd(38)} (unavailable)\n`;
    }
  }));

  // --- Section 3: Job Pipeline ---
  let pipelineLines = '';

  // all_jobs.json — fetch from GitHub (always fresh, not local stale copy)
  const allJobsRaw = await rawGet(`https://raw.githubusercontent.com/${ORG}/jobs-data-2026/main/.github/data/all_jobs.json`);
  if (allJobsRaw) {
    const count = allJobsRaw.split('\n').filter(l => l.trim()).length;
    pipelineLines += `${'all_jobs.json (pipeline total)'.padEnd(36)} ${fmtNum(count)}\n`;
  } else {
    pipelineLines += `${'all_jobs.json (pipeline total)'.padEnd(36)} (unavailable)\n`;
  }

  // Consumer repos via raw URL
  for (const { repo, label } of CONSUMER_REPOS) {
    const raw = await rawGet(`https://raw.githubusercontent.com/${ORG}/${repo}/main/.github/data/current_jobs.json`);
    let count = '?';
    if (raw) {
      try { count = fmtNum(JSON.parse(raw).length); } catch {}
    }
    pipelineLines += `${label.padEnd(36)} ${count}\n`;
  }

  // Discord posted last 24h — read from posted_jobs.json
  try {
    const postedFile = path.join(process.cwd(), '.github', 'data', 'posted_jobs.json');
    const posted = JSON.parse(fs.readFileSync(postedFile, 'utf8'));
    const jobs = Array.isArray(posted.jobs) ? posted.jobs : [];
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    const recentCount = jobs.filter(j => j.postedToDiscord && new Date(j.postedToDiscord).getTime() > cutoff).length;
    pipelineLines += `${'Discord posted (last 24h)'.padEnd(36)} ${recentCount}\n`;
  } catch {
    pipelineLines += `${'Discord posted (last 24h)'.padEnd(36)} ?\n`;
  }

  // --- Build messages ---
  const msg1 = `📊 **zapplyjobs — ${today} — Daily Report**\n\n⭐ **STARS**\n\`\`\`\n${starsLines}\nTotal: ${fmtNum(totalStars)} stars (${deltaStr})\n\`\`\``;
  const msg2 = `🤖 **WORKFLOW HEALTH (last 24h)**\n\`\`\`\n${workflowLines || '(no data)\n'}\`\`\``;
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

  console.log('✅ Daily stats posted to Discord');
  await client.destroy();

  // --- Persist today's star counts ---
  const newStats = { date: new Date().toISOString(), stars: {} };
  for (const name of STAR_REPOS) {
    if (repoMap[name]) newStats.stars[name] = repoMap[name].stargazers_count;
  }
  fs.writeFileSync(STATS_FILE, JSON.stringify(newStats, null, 2), 'utf8');
  console.log('✅ Saved daily-stats.json');
}

main().catch(err => {
  console.error('❌ Fatal error:', err.message);
  process.exit(1);
});
