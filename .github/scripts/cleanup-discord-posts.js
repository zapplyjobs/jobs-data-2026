#!/usr/bin/env node

/**
 * Cleanup Discord Posts
 *
 * Three modes (mutually exclusive, checked in priority order):
 *   WINDOW mode      — delete messages between WINDOW_OLDER_THAN_HOURS and WINDOW_NEWER_THAN_HOURS ago
 *                      e.g. WINDOW_OLDER_THAN_HOURS=1 WINDOW_NEWER_THAN_HOURS=3 → delete posted 1–3 hours ago
 *   LAST_N_HOURS     — delete messages from the last N hours (e.g. undo recent posts)
 *   OLDER_THAN_HOURS — delete messages older than N hours (default: 336 = 14 days)
 *
 * Default: dry-run mode (set DRY_RUN=false to actually delete).
 *
 * Usage (via workflow_dispatch only — no cron schedule):
 *   WINDOW_OLDER_THAN_HOURS=1   (window mode: delete messages at least 1 hour old...)
 *   WINDOW_NEWER_THAN_HOURS=3   (...but no more than 3 hours old — both required for window mode)
 *   LAST_N_HOURS=1              (delete messages posted in the last 1 hour; ignored if window mode active)
 *   OLDER_THAN_HOURS=336        (default: 14 days; ignored if LAST_N_HOURS or window mode set)
 *   DRY_RUN=true                (default: true — set to false to actually delete)
 *   CHANNEL_IDS=id1,id2         (optional: specific channels only, empty = all 23 channels)
 */

const { Client, GatewayIntentBits } = require('discord.js');
const fs = require('fs');
const path = require('path');

const WINDOW_OLDER_THAN_HOURS = process.env.WINDOW_OLDER_THAN_HOURS ? parseFloat(process.env.WINDOW_OLDER_THAN_HOURS) : null;
const WINDOW_NEWER_THAN_HOURS = process.env.WINDOW_NEWER_THAN_HOURS ? parseFloat(process.env.WINDOW_NEWER_THAN_HOURS) : null;
const WINDOW_MODE = WINDOW_OLDER_THAN_HOURS !== null && WINDOW_NEWER_THAN_HOURS !== null;
const LAST_N_HOURS = (!WINDOW_MODE && process.env.LAST_N_HOURS) ? parseInt(process.env.LAST_N_HOURS) : null;
const OLDER_THAN_HOURS = parseInt(process.env.OLDER_THAN_HOURS) || 336; // 14 days
const DRY_RUN = process.env.DRY_RUN !== 'false'; // default true
const REMOVE_FROM_POSTED = process.env.REMOVE_FROM_POSTED === 'true'; // default false
const SPECIFIC_CHANNELS = process.env.CHANNEL_IDS ? process.env.CHANNEL_IDS.split(',').map(s => s.trim()).filter(Boolean) : [];

// All 23 active channel IDs (New-Grad: 11, Internships: 12)
const ALL_CHANNELS = [
  // New-Grad industry
  process.env.DISCORD_TECH_CHANNEL_ID,
  process.env.DISCORD_AI_CHANNEL_ID,
  process.env.DISCORD_DS_CHANNEL_ID,
  process.env.DISCORD_FINANCE_CHANNEL_ID,
  process.env.DISCORD_NURSING_CHANNEL_ID,
  process.env.DISCORD_OTHER_INDUSTRY_CHANNEL_ID,
  // New-Grad location
  process.env.DISCORD_BAY_AREA_CHANNEL_ID,
  process.env.DISCORD_NY_CHANNEL_ID,
  process.env.DISCORD_PNW_CHANNEL_ID,
  process.env.DISCORD_REMOTE_USA_CHANNEL_ID,
  process.env.DISCORD_OTHER_USA_CHANNEL_ID,
  // Internships industry
  process.env.DISCORD_TECH_INT_CHANNEL_ID,
  process.env.DISCORD_AI_INT_CHANNEL_ID,
  process.env.DISCORD_DS_INT_CHANNEL_ID,
  process.env.DISCORD_SALES_INT_CHANNEL_ID,
  process.env.DISCORD_MARKETING_INT_CHANNEL_ID,
  process.env.DISCORD_OTHER_INDUSTRY_INT_CHANNEL_ID,
  // Internships location
  process.env.DISCORD_REMOTE_USA_INT_CHANNEL_ID,
  process.env.DISCORD_BAY_AREA_INT_CHANNEL_ID,
  process.env.DISCORD_NY_INT_CHANNEL_ID,
  process.env.DISCORD_PNW_INT_CHANNEL_ID,
  process.env.DISCORD_SOCAL_INT_CHANNEL_ID,
  process.env.DISCORD_OTHER_USA_INT_CHANNEL_ID,
].filter(Boolean);

const CHANNEL_IDS = SPECIFIC_CHANNELS.length > 0 ? SPECIFIC_CHANNELS : ALL_CHANNELS;
const BULK_DELETE_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000; // Discord: bulk delete only for <14d messages

async function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function cleanChannel(channel, cutoffDate, newerThanDate, windowStart, windowEnd) {
  let scanned = 0;
  let deleted = 0;
  let skipped = 0;
  let lastId = null;
  const deletedMessageIds = [];

  console.log(`  📋 ${channel.name} (${channel.id})`);

  while (true) {
    const options = { limit: 100 };
    if (lastId) options.before = lastId;

    const messages = await channel.messages.fetch(options);
    if (messages.size === 0) break;

    // WINDOW mode: delete messages between windowStart and windowEnd (older boundary to newer boundary)
    // LAST_N_HOURS mode: delete messages newer than newerThanDate, stop scanning once we pass it
    // OLDER_THAN_HOURS mode: delete messages older than cutoffDate
    let toDelete, toSkip;
    if (windowStart && windowEnd) {
      toDelete = messages.filter(m => m.createdAt <= windowStart && m.createdAt >= windowEnd);
      toSkip = messages.filter(m => m.createdAt > windowStart || m.createdAt < windowEnd);
      // Stop scanning once all messages are older than our window
      if (messages.every(m => m.createdAt < windowEnd)) break;
    } else if (newerThanDate) {
      toDelete = messages.filter(m => m.createdAt >= newerThanDate);
      toSkip = messages.filter(m => m.createdAt < newerThanDate);
      // Once all messages in this batch are older than our window, stop scanning
      if (messages.every(m => m.createdAt < newerThanDate)) break;
    } else {
      toDelete = messages.filter(m => m.createdAt < cutoffDate);
      toSkip = messages.filter(m => m.createdAt >= cutoffDate);
    }

    scanned += messages.size;
    skipped += toSkip.size;

    if (toDelete.size === 0) break;

    if (DRY_RUN) {
      console.log(`    [DRY RUN] Would delete ${toDelete.size} messages`);
      deleted += toDelete.size;
      toDelete.forEach(m => deletedMessageIds.push(m.id));
    } else {
      // Split into bulk-deletable (<14d) and individual (>=14d old)
      const now = Date.now();
      const bulkEligible = toDelete.filter(m => (now - m.createdTimestamp) < BULK_DELETE_MAX_AGE_MS);
      const individual = toDelete.filter(m => (now - m.createdTimestamp) >= BULK_DELETE_MAX_AGE_MS);

      if (bulkEligible.size > 0) {
        await channel.bulkDelete(bulkEligible, true);
        bulkEligible.forEach(m => deletedMessageIds.push(m.id));
        deleted += bulkEligible.size;
        await sleep(1000);
      }

      for (const [, msg] of individual) {
        try {
          await msg.delete();
          deletedMessageIds.push(msg.id);
          deleted++;
          await sleep(1000); // 1 req/sec for old messages
        } catch (err) {
          console.log(`    ⚠️  Failed to delete message ${msg.id}: ${err.message}`);
        }
      }
    }

    lastId = messages.last()?.id;
    if (messages.size < 100) break;
  }

  console.log(`    Scanned: ${scanned} | Deleted: ${deleted} | Kept: ${skipped}`);
  return { scanned, deleted, skipped, deletedMessageIds };
}

async function main() {
  const now = Date.now();
  const newerThanDate = LAST_N_HOURS ? new Date(now - LAST_N_HOURS * 60 * 60 * 1000) : null;
  const cutoffDate = new Date(now - OLDER_THAN_HOURS * 60 * 60 * 1000);
  // Window: windowStart = older boundary (further back), windowEnd = newer boundary (more recent)
  const windowStart = WINDOW_MODE ? new Date(now - WINDOW_NEWER_THAN_HOURS * 60 * 60 * 1000) : null;
  const windowEnd   = WINDOW_MODE ? new Date(now - WINDOW_OLDER_THAN_HOURS * 60 * 60 * 1000) : null;

  console.log(`🧹 Discord Cleanup — ${DRY_RUN ? 'DRY RUN (no deletions)' : 'LIVE MODE'}`);
  if (WINDOW_MODE) {
    console.log(`   Mode: WINDOW — messages between ${WINDOW_OLDER_THAN_HOURS}h ago and ${WINDOW_NEWER_THAN_HOURS}h ago`);
    console.log(`   Window: ${windowEnd.toISOString()} → ${windowStart.toISOString()}`);
  } else if (newerThanDate) {
    console.log(`   Mode: LAST_N_HOURS=${LAST_N_HOURS} (messages newer than ${newerThanDate.toISOString()})`);
  } else {
    console.log(`   Mode: OLDER_THAN_HOURS=${OLDER_THAN_HOURS} (messages older than ${cutoffDate.toISOString()})`);
  }
  console.log(`   Channels: ${CHANNEL_IDS.length} (${SPECIFIC_CHANNELS.length > 0 ? 'specific' : 'all'})`);
  console.log('');

  const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages] });

  await client.login(process.env.DISCORD_TOKEN);
  await new Promise(r => client.once('ready', r));
  console.log(`✅ Logged in as ${client.user.tag}\n`);

  let totalScanned = 0, totalDeleted = 0, totalSkipped = 0;
  const allDeletedMessageIds = [];

  for (const channelId of CHANNEL_IDS) {
    try {
      const channel = await client.channels.fetch(channelId);
      if (!channel || !channel.isTextBased()) {
        console.log(`  ⚠️  Channel ${channelId} not found or not text-based`);
        continue;
      }
      const result = await cleanChannel(channel, cutoffDate, newerThanDate, windowStart, windowEnd);
      totalScanned += result.scanned;
      totalDeleted += result.deleted;
      totalSkipped += result.skipped;
      allDeletedMessageIds.push(...result.deletedMessageIds);
    } catch (err) {
      console.log(`  ❌ Error on channel ${channelId}: ${err.message}`);
    }
  }

  console.log('\n━━━ Summary ━━━');
  console.log(`Total scanned: ${totalScanned}`);
  console.log(`Total deleted: ${totalDeleted}${DRY_RUN ? ' (dry run — no actual deletions)' : ''}`);
  console.log(`Total kept:    ${totalSkipped}`);

  if (REMOVE_FROM_POSTED && allDeletedMessageIds.length > 0) {
    removeFromPostedJobs(allDeletedMessageIds);
  } else if (REMOVE_FROM_POSTED && allDeletedMessageIds.length === 0) {
    console.log('\n📝 remove_from_posted: no messages deleted, nothing to remove from posted_jobs.json');
  }

  await client.destroy();
}

/**
 * Remove deleted messages from posted_jobs.json AND global-dedupe-store.json
 * so they can be reposted by the next cron run.
 *
 * posted_jobs.json: matched by discordPosts[channelId].messageId
 * global-dedupe-store.json: matched by fingerprints[fp].messageId
 * Both must be cleared — poster checks global dedupe first and will skip if only posted_jobs is cleared.
 */
function removeFromPostedJobs(deletedMessageIds) {
  const dataDir = path.join(process.cwd(), '.github', 'data');
  const postedPath = path.join(dataDir, 'posted_jobs.json');
  const dedupePath = path.join(dataDir, 'global-dedupe-store.json');
  const deletedSet = new Set(deletedMessageIds);

  // --- posted_jobs.json ---
  if (!fs.existsSync(postedPath)) {
    console.log('\n📝 remove_from_posted: posted_jobs.json not found, skipping');
  } else {
    const data = JSON.parse(fs.readFileSync(postedPath, 'utf8'));
    const before = data.jobs.length;
    let removedJobs = 0;
    let removedChannels = 0;
    const removedJobRecords = []; // snapshots of removed jobs (before discordPosts is mutated)

    data.jobs = data.jobs.filter(job => {
      if (!job.discordPosts) return true;

      // Identify which channels are being deleted for this job
      const channelsBeingDeleted = Object.keys(job.discordPosts).filter(
        channelId => deletedSet.has(job.discordPosts[channelId].messageId)
      );

      if (channelsBeingDeleted.length === 0) return true;

      // Snapshot the channels being removed before mutating
      const snapshot = { discordPosts: {} };
      for (const channelId of channelsBeingDeleted) {
        snapshot.discordPosts[channelId] = job.discordPosts[channelId];
        delete job.discordPosts[channelId];
        removedChannels++;
      }

      if (Object.keys(job.discordPosts).length === 0) {
        removedJobRecords.push(snapshot);
        removedJobs++;
        return false;
      }
      return true;
    });

    // Recalculate counters: subtract the number of deleted jobs per channel from current counter.
    // We count how many deleted jobs had a channelJobNumber in each channel, then subtract.
    // This is safe because posted_jobs.json only has a 7-day active window — scanning for max
    // would give a falsely low number for channels with historically seeded counters.
    const deletedCountPerChannel = {};
    for (const job of removedJobRecords) {
      if (!job.discordPosts) continue;
      for (const channelId of Object.keys(job.discordPosts)) {
        deletedCountPerChannel[channelId] = (deletedCountPerChannel[channelId] || 0) + 1;
      }
    }
    const updatedCounters = {};
    for (const [channelId, deletedCount] of Object.entries(deletedCountPerChannel)) {
      const current = data.metadata.channelJobNumbers[channelId] || 0;
      const newVal = Math.max(0, current - deletedCount);
      data.metadata.channelJobNumbers[channelId] = newVal;
      updatedCounters[channelId] = { from: current, to: newVal };
    }

    data.metadata.totalJobs = data.jobs.length;
    data.lastUpdated = new Date().toISOString();

    if (DRY_RUN) {
      console.log(`\n📝 [DRY RUN] posted_jobs.json: would remove ${removedJobs} job entries (${removedChannels} channel postings)`);
      if (Object.keys(updatedCounters).length > 0) {
        console.log(`📝 [DRY RUN] counters would reset:`, JSON.stringify(updatedCounters));
      }
    } else {
      fs.writeFileSync(postedPath, JSON.stringify(data, null, 2) + '\n');
      console.log(`\n📝 posted_jobs.json: removed ${removedJobs} job entries (${removedChannels} channel postings)`);
      console.log(`   Before: ${before} jobs → After: ${data.jobs.length} jobs`);
      if (Object.keys(updatedCounters).length > 0) {
        console.log(`📝 Counters reset to last remaining job number:`);
        for (const [ch, v] of Object.entries(updatedCounters)) {
          console.log(`   Channel ${ch}: ${v.from} → ${v.to}`);
        }
      }
    }
  }

  // --- global-dedupe-store.json ---
  if (!fs.existsSync(dedupePath)) {
    console.log('📝 remove_from_posted: global-dedupe-store.json not found, skipping');
  } else {
    const dedupeData = JSON.parse(fs.readFileSync(dedupePath, 'utf8'));
    const beforeDedupeCount = Object.keys(dedupeData.fingerprints).length;
    let removedFingerprints = 0;

    for (const [fp, entry] of Object.entries(dedupeData.fingerprints)) {
      if (deletedSet.has(entry.messageId)) {
        delete dedupeData.fingerprints[fp];
        removedFingerprints++;
      }
    }

    dedupeData.lastUpdated = new Date().toISOString();

    if (DRY_RUN) {
      console.log(`📝 [DRY RUN] global-dedupe-store.json: would remove ${removedFingerprints} fingerprints`);
    } else {
      fs.writeFileSync(dedupePath, JSON.stringify(dedupeData, null, 2) + '\n');
      console.log(`📝 global-dedupe-store.json: removed ${removedFingerprints} fingerprints`);
      console.log(`   Before: ${beforeDedupeCount} → After: ${Object.keys(dedupeData.fingerprints).length}`);
    }
  }
}

main().catch(err => {
  console.error('❌ Fatal error:', err.message);
  process.exit(1);
});
