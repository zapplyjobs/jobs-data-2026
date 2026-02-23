#!/usr/bin/env node

/**
 * Cleanup Discord Posts
 *
 * Two modes (mutually exclusive):
 *   OLDER_THAN_HOURS  — delete messages older than N hours (default: 336 = 14 days)
 *   LAST_N_HOURS      — delete messages from the last N hours (e.g. undo recent posts)
 *
 * Default: dry-run mode (set DRY_RUN=false to actually delete).
 *
 * Usage (via workflow_dispatch only — no cron schedule):
 *   OLDER_THAN_HOURS=336  (default: 14 days; ignored if LAST_N_HOURS is set)
 *   LAST_N_HOURS=1        (delete messages posted in the last 1 hour)
 *   DRY_RUN=true          (default: true — set to false to actually delete)
 *   CHANNEL_IDS=id1,id2   (optional: specific channels only, empty = all 23 channels)
 */

const { Client, GatewayIntentBits } = require('discord.js');

const LAST_N_HOURS = process.env.LAST_N_HOURS ? parseInt(process.env.LAST_N_HOURS) : null;
const OLDER_THAN_HOURS = parseInt(process.env.OLDER_THAN_HOURS) || 336; // 14 days
const DRY_RUN = process.env.DRY_RUN !== 'false'; // default true
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

async function cleanChannel(channel, cutoffDate, newerThanDate) {
  let scanned = 0;
  let deleted = 0;
  let skipped = 0;
  let lastId = null;

  console.log(`  📋 ${channel.name} (${channel.id})`);

  while (true) {
    const options = { limit: 100 };
    if (lastId) options.before = lastId;

    const messages = await channel.messages.fetch(options);
    if (messages.size === 0) break;

    // LAST_N_HOURS mode: delete messages newer than newerThanDate, stop scanning once we pass it
    // OLDER_THAN_HOURS mode: delete messages older than cutoffDate
    let toDelete, toSkip;
    if (newerThanDate) {
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
    } else {
      // Split into bulk-deletable (<14d) and individual (>=14d old)
      const now = Date.now();
      const bulkEligible = toDelete.filter(m => (now - m.createdTimestamp) < BULK_DELETE_MAX_AGE_MS);
      const individual = toDelete.filter(m => (now - m.createdTimestamp) >= BULK_DELETE_MAX_AGE_MS);

      if (bulkEligible.size > 0) {
        await channel.bulkDelete(bulkEligible, true);
        deleted += bulkEligible.size;
        await sleep(1000);
      }

      for (const [, msg] of individual) {
        try {
          await msg.delete();
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
  return { scanned, deleted, skipped };
}

async function main() {
  const newerThanDate = LAST_N_HOURS ? new Date(Date.now() - LAST_N_HOURS * 60 * 60 * 1000) : null;
  const cutoffDate = new Date(Date.now() - OLDER_THAN_HOURS * 60 * 60 * 1000);

  console.log(`🧹 Discord Cleanup — ${DRY_RUN ? 'DRY RUN (no deletions)' : 'LIVE MODE'}`);
  if (newerThanDate) {
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

  for (const channelId of CHANNEL_IDS) {
    try {
      const channel = await client.channels.fetch(channelId);
      if (!channel || !channel.isTextBased()) {
        console.log(`  ⚠️  Channel ${channelId} not found or not text-based`);
        continue;
      }
      const result = await cleanChannel(channel, cutoffDate, newerThanDate);
      totalScanned += result.scanned;
      totalDeleted += result.deleted;
      totalSkipped += result.skipped;
    } catch (err) {
      console.log(`  ❌ Error on channel ${channelId}: ${err.message}`);
    }
  }

  console.log('\n━━━ Summary ━━━');
  console.log(`Total scanned: ${totalScanned}`);
  console.log(`Total deleted: ${totalDeleted}${DRY_RUN ? ' (dry run — no actual deletions)' : ''}`);
  console.log(`Total kept:    ${totalSkipped}`);

  await client.destroy();
}

main().catch(err => {
  console.error('❌ Fatal error:', err.message);
  process.exit(1);
});
