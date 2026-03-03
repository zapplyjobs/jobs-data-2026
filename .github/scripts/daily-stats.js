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

// Consumer repos that write current_jobs.json (New-Grad excluded — no data file by design)
const CONSUMER_REPOS = [
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
  if (previous == null) return '(new)';
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

  // GitHub system workflows to exclude (not our code)
  const SYSTEM_WORKFLOWS = ['pages build and deployment', 'pages-build-deployment', 'CodeQL'];

  const workflowResults = await Promise.all(PIPELINE_REPOS.map(async (repo) => {
    try {
      const runs = await githubGet(`/repos/${ORG}/${repo}/actions/runs?per_page=100&created=>=${since}`);
      const list = (runs.workflow_runs || []).filter(r => !SYSTEM_WORKFLOWS.includes(r.name));
      const total = list.length;
      const success = list.filter(r => r.conclusion === 'success').length;
      const fail = list.filter(r => r.conclusion === 'failure').length;
      const inProgress = list.filter(r => r.status === 'in_progress').length;
      const status = fail > 0 ? '⚠️' : (total === 0 ? '➖' : '✅');
      const label = repo.slice(0, 38).padEnd(38);
      return `${status} ${label} ${success}✅ ${fail}❌ ${inProgress > 0 ? `${inProgress}🔄` : '  '} (${total} runs)\n`;
    } catch {
      return `➖ ${repo.slice(0, 38).padEnd(38)} (unavailable)\n`;
    }
  }));
  workflowLines = workflowResults.join('');

  // --- Section 3: Job Pipeline ---
  let pipelineLines = '';

  // Pipeline total — read local all_jobs.json (lives in this repo, always current)
  const allJobsPath = path.join(process.cwd(), '.github', 'data', 'all_jobs.json');
  let pipelineTotal = null;
  if (fs.existsSync(allJobsPath)) {
    pipelineTotal = fs.readFileSync(allJobsPath, 'utf8').split('\n').filter(l => l.trim()).length;
  }

  // Delta vs yesterday + anomaly check
  const prevTotal = prevStats.pipelineTotal ?? null;
  const history = Array.isArray(prevStats.history) ? prevStats.history : [];
  let totalLine = pipelineTotal != null ? fmtNum(pipelineTotal) : '(unavailable)';
  if (pipelineTotal != null && prevTotal != null) {
    const d = pipelineTotal - prevTotal;
    totalLine += d === 0 ? ' (=)' : (d > 0 ? ` (+${d})` : ` (${d})`);
  }
  let anomalyFlag = '';
  if (pipelineTotal != null && history.length >= 3) {
    const avg = history.reduce((s, v) => s + v, 0) / history.length;
    if (pipelineTotal < avg * 0.85) anomalyFlag = ' ⚠️ COUNT DROP';
  }
  pipelineLines += `${'Pipeline total'.padEnd(28)} ${totalLine}${anomalyFlag}\n`;

  // Per-source breakdown — from local jobs-metadata.json
  try {
    const meta = JSON.parse(fs.readFileSync(path.join(process.cwd(), '.github', 'data', 'jobs-metadata.json'), 'utf8'));
    if (meta.by_source) {
      const src = meta.by_source;
      const parts = ['workday','greenhouse','jsearch','ashby','lever']
        .filter(k => src[k] != null)
        .map(k => `${k[0].toUpperCase()}${k.slice(1)}: ${fmtNum(src[k])}`);
      pipelineLines += `${'By source'.padEnd(28)} ${parts.join(' | ')}\n`;
    }
  } catch { /* metadata unavailable — skip */ }

  // enriched_jobs.json count (local JSONL — jobs-data-2026 layer, consumed by enrichment API users)
  const enrichedPath = path.join(process.cwd(), '.github', 'data', 'enriched_jobs.json');
  if (fs.existsSync(enrichedPath)) {
    const enrichedCount = fs.readFileSync(enrichedPath, 'utf8').split('\n').filter(l => l.trim()).length;
    pipelineLines += `${'Enriched jobs'.padEnd(28)} ${fmtNum(enrichedCount)}\n`;
  }

  // Consumer counts + freshness — fetch current_jobs.json for count, commit API for age
  // New-Grad has no current_jobs.json by design — show pipeline total instead
  pipelineLines += '\n';
  const freshnessResults = await Promise.all(CONSUMER_REPOS.map(async ({ repo, label }) => {
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
  pipelineLines += `${'Consumer jobs / freshness'.padEnd(28)}\n` + freshnessResults.join('\n') + '\n';

  // Discord posted last 24h
  try {
    const postedFile = path.join(process.cwd(), '.github', 'data', 'posted_jobs.json');
    const posted = JSON.parse(fs.readFileSync(postedFile, 'utf8'));
    const jobs = Array.isArray(posted.jobs) ? posted.jobs : [];
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    const recentCount = jobs.filter(j => j.postedToDiscord && new Date(j.postedToDiscord).getTime() > cutoff).length;
    pipelineLines += `${'Discord posted (last 24h)'.padEnd(28)} ${recentCount}\n`;
  } catch {
    pipelineLines += `${'Discord posted (last 24h)'.padEnd(28)} ?\n`;
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

  // --- Persist today's stats (stars + pipeline total for delta/anomaly) ---
  const newHistory = pipelineTotal != null
    ? [...history, pipelineTotal].slice(-7)  // keep last 7 days
    : history;
  const newStats = { date: new Date().toISOString(), stars: {}, pipelineTotal, history: newHistory };
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
