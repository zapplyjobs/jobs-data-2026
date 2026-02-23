#!/usr/bin/env node

/**
 * Pipeline Alert
 *
 * Checks 6 failure modes and posts a Discord alert if any fail.
 * Silent on all-green — only fires when something is actually wrong.
 *
 * Failure modes checked:
 *   1. fetch-jobs.yml stale (last run > 30 min ago or failed)
 *   2. post-to-discord.yml last run failed
 *   3. Any consumer update-jobs.yml failed
 *   4. all_jobs.json job count dropped >20% vs previous snapshot
 *   5. JSearch remaining_today = 0 (quota exhausted)
 *   6. us-tagged job count = 0 (location tagger broken)
 *
 * Not alerts (by design, not failures):
 *   - posted_jobs count = 0 per run (dedup saturation is normal)
 *   - New-Grad job count = 0 (no current_jobs.json by design)
 *   - cleanup-discord-posts.yml not running (manual only)
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const { Client, GatewayIntentBits } = require('discord.js');

const GITHUB_TOKEN = process.env.GITHUB_TOKEN;
const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const CHANNEL_ID = process.env.DISCORD_DAILY_STATS_CHANNEL_ID;
const DATA_DIR = path.join(process.cwd(), '.github', 'data');
const METRICS_LATEST = path.join(DATA_DIR, 'metrics', 'latest.json');

const CONSUMER_REPOS = [
  'New-Grad-Jobs-2026',
  'Internships-2026',
  'New-Grad-Software-Engineering-Jobs-2026',
  'New-Grad-Data-Science-Jobs-2026',
  'New-Grad-Hardware-Engineering-Jobs-2026',
  'New-Grad-Nursing-Jobs-2026',
];

const STALE_THRESHOLD_MS = 30 * 60 * 1000; // 30 minutes

function ghRequest(url) {
  return new Promise((resolve, reject) => {
    https.get(url, {
      headers: {
        'User-Agent': 'Zapply-Pipeline-Alert',
        'Authorization': `Bearer ${GITHUB_TOKEN}`,
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

async function getLastWorkflowRun(owner, repo, workflowFile) {
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowFile}/runs?per_page=1`;
  try {
    const res = await ghRequest(url);
    if (res.status !== 200 || !res.body?.workflow_runs?.length) return null;
    return res.body.workflow_runs[0];
  } catch {
    return null;
  }
}

async function runChecks() {
  const failures = [];
  const now = Date.now();

  // Check 1: fetch-jobs.yml stale or failed
  const fetchRun = await getLastWorkflowRun('zapplyjobs', 'jobs-aggregator-private', 'fetch-jobs.yml');
  if (!fetchRun) {
    failures.push('**fetch-jobs.yml**: No runs found');
  } else if (fetchRun.conclusion === 'failure') {
    failures.push(`**fetch-jobs.yml**: Last run failed (<t:${Math.floor(new Date(fetchRun.updated_at).getTime() / 1000)}:R>)`);
  } else {
    const age = now - new Date(fetchRun.updated_at).getTime();
    if (age > STALE_THRESHOLD_MS) {
      const mins = Math.floor(age / 60000);
      failures.push(`**fetch-jobs.yml**: Last run ${mins}m ago (expected ≤30m)`);
    }
  }

  // Check 2: post-to-discord.yml failed
  const discordRun = await getLastWorkflowRun('zapplyjobs', 'jobs-data-2026', 'post-to-discord.yml');
  if (!discordRun) {
    failures.push('**post-to-discord.yml**: No runs found');
  } else if (discordRun.conclusion === 'failure') {
    failures.push(`**post-to-discord.yml**: Last run failed (<t:${Math.floor(new Date(discordRun.updated_at).getTime() / 1000)}:R>)`);
  }

  // Check 3: consumer update-jobs.yml failures
  const consumerChecks = await Promise.all(
    CONSUMER_REPOS.map(async repo => {
      const run = await getLastWorkflowRun('zapplyjobs', repo, 'update-jobs.yml');
      if (run?.conclusion === 'failure') return repo;
      return null;
    })
  );
  const failedConsumers = consumerChecks.filter(Boolean);
  if (failedConsumers.length > 0) {
    failures.push(`**update-jobs.yml failed**: ${failedConsumers.join(', ')}`);
  }

  // Checks 4–6: read from local jobs-metadata.json (available at runtime in jobs-data-2026)
  const metadataPath = path.join(DATA_DIR, 'jobs-metadata.json');
  if (!fs.existsSync(metadataPath)) {
    failures.push('**jobs-metadata.json**: File missing — pipeline may not be running');
  } else {
    const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));

    // Check 4: job count dropped >20% vs previous snapshot
    if (fs.existsSync(METRICS_LATEST)) {
      try {
        const prev = JSON.parse(fs.readFileSync(METRICS_LATEST, 'utf8'));
        const prevTotal = prev?.pipeline?.pipelineTotal;
        const currTotal = metadata.total_jobs;
        if (prevTotal && currTotal && currTotal < prevTotal * 0.8) {
          failures.push(`**Job count drop**: ${currTotal} jobs (was ${prevTotal}, dropped ${Math.round((1 - currTotal/prevTotal)*100)}%)`);
        }
      } catch {
        // Metrics file unreadable — skip this check
      }
    }

    // Check 5: JSearch quota exhausted
    const remaining = metadata.jsearch_stats?.remaining_today;
    if (remaining === 0) {
      failures.push('**JSearch quota**: remaining_today = 0, all JSearch fetching stopped');
    }

    // Check 6: us-tagged count = 0
    const usTagged = metadata.tag_stats?.locations?.us;
    if (usTagged === 0) {
      failures.push('**US location tagger broken**: 0 jobs tagged `us` — check tagLocations() in tag-engine.js');
    }
  }

  return failures;
}

async function postAlert(failures) {
  if (!DISCORD_TOKEN || !CHANNEL_ID) {
    console.error('DISCORD_TOKEN or DISCORD_DAILY_STATS_CHANNEL_ID not set');
    process.exit(1);
  }

  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  await client.login(DISCORD_TOKEN);
  await new Promise(r => client.once('ready', r));

  const channel = await client.channels.fetch(CHANNEL_ID);

  const embed = {
    title: '🚨 Pipeline Alert',
    color: 0xe74c3c, // red
    description: failures.map(f => `• ${f}`).join('\n'),
    footer: { text: `Checked at ${new Date().toISOString()}` }
  };

  await channel.send({ embeds: [embed] });
  await client.destroy();
}

async function main() {
  console.log('🔍 Running pipeline health checks...');

  if (!GITHUB_TOKEN) { console.error('GITHUB_TOKEN not set'); process.exit(1); }

  const failures = await runChecks();

  if (failures.length === 0) {
    console.log('✅ All checks passed — no alert sent');
    return;
  }

  console.log(`⚠️  ${failures.length} check(s) failed:`);
  failures.forEach(f => console.log(`   • ${f}`));

  await postAlert(failures);
  console.log('✅ Alert posted to Discord');
}

main().catch(err => {
  console.error('❌ Fatal:', err.message);
  process.exit(1);
});
