#!/usr/bin/env node

/**
 * One-off: delete specific Discord messages by ID and remove from posted_jobs.json
 * so they get reposted with correct channel job numbers.
 *
 * Usage: node .github/scripts/delete-specific-messages.js
 * Set DRY_RUN=false to actually delete.
 */

const { Client, GatewayIntentBits } = require('discord.js');
const fs = require('fs');
const path = require('path');

const DRY_RUN = process.env.DRY_RUN !== 'false';
const DATA_DIR = path.join(process.cwd(), '.github', 'data');

// Messages to delete: { channelId, messageId, jobTitle }
const MESSAGES_TO_DELETE = [
  // tech-internships (wrong job numbers #1, #2, #3 — should be #255+)
  { channelId: '1464303178286760059', messageId: '1475538754767683676', label: 'Design Strategist Intern (tech-int #1)' },
  { channelId: '1464303178286760059', messageId: '1475546743742402704', label: 'Info Security Intern (tech-int #2)' },
  { channelId: '1464303178286760059', messageId: '1475546756988010647', label: 'Product & Tech Analytics Intern (tech-int #3)' },
  // new-york-internships (wrong job numbers #4, #5, #6, #7 — should be #21+)
  { channelId: '1464303539101634634', messageId: '1475538756772823070', label: 'Design Strategist Intern (ny-int #4)' },
  { channelId: '1464303539101634634', messageId: '1475546751229362359', label: 'Legal Intern (ny-int #5)' },
  { channelId: '1464303539101634634', messageId: '1475546759294877857', label: 'Product & Tech Analytics Intern (ny-int #6)' },
  { channelId: '1464303539101634634', messageId: '1475546763149574321', label: 'Research Intern (ny-int #7)' },
];

// Company to remove from posted_jobs.json (so they repost)
const COMPANY_TO_REMOVE = 'mongodb';

async function main() {
  console.log(`🗑️  Delete Specific Messages — ${DRY_RUN ? 'DRY RUN' : 'LIVE MODE'}`);
  console.log(`   Messages to delete: ${MESSAGES_TO_DELETE.length}`);
  console.log('');

  // Step 1: Delete from Discord
  const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages] });
  await client.login(process.env.DISCORD_TOKEN);
  await new Promise(r => client.once('ready', r));
  console.log(`✅ Logged in as ${client.user.tag}\n`);

  for (const { channelId, messageId, label } of MESSAGES_TO_DELETE) {
    try {
      const channel = await client.channels.fetch(channelId);
      if (!channel) { console.log(`  ❌ Channel ${channelId} not found`); continue; }

      if (DRY_RUN) {
        console.log(`  [DRY RUN] Would delete: ${label} (msg ${messageId})`);
      } else {
        const msg = await channel.messages.fetch(messageId);
        await msg.delete();
        console.log(`  ✅ Deleted: ${label}`);
        await new Promise(r => setTimeout(r, 500));
      }
    } catch (err) {
      console.log(`  ⚠️  ${label}: ${err.message}`);
    }
  }

  await client.destroy();

  // Step 2: Remove from posted_jobs.json so they repost
  console.log('\n📝 Updating posted_jobs.json...');
  const postedPath = path.join(DATA_DIR, 'posted_jobs.json');
  const data = JSON.parse(fs.readFileSync(postedPath, 'utf8'));

  const before = data.jobs.length;
  const removed = [];

  data.jobs = data.jobs.filter(j => {
    const company = (j.company || '').toLowerCase();
    if (company.includes(COMPANY_TO_REMOVE)) {
      removed.push(j.title || j.id);
      return false;
    }
    return true;
  });

  data.metadata.totalJobs = data.jobs.length;

  // Reset the internship channel counters back to correct floor
  // (already seeded to historical max — the wrong new-bot posts inflated them slightly)
  const CORRECT_FLOORS = {
    '1464303178286760059': 254, // tech-internships
    '1464303539101634634': 20,  // new-york-internships
  };
  for (const [cid, floor] of Object.entries(CORRECT_FLOORS)) {
    data.metadata.channelJobNumbers[cid] = floor;
  }

  if (DRY_RUN) {
    console.log(`  [DRY RUN] Would remove ${removed.length} jobs from posted_jobs.json:`);
    removed.forEach(t => console.log(`    - ${t}`));
    console.log(`  [DRY RUN] Would reset tech-int counter to 254, ny-int to 20`);
  } else {
    fs.writeFileSync(postedPath, JSON.stringify(data, null, 2) + '\n');
    console.log(`  ✅ Removed ${removed.length} jobs: ${removed.join(', ')}`);
    console.log(`  ✅ Reset tech-int → 254, ny-int → 20`);
    console.log(`  ✅ Saved posted_jobs.json (${data.jobs.length} jobs remaining)`);
  }

  console.log('\n✅ Done. Next discord-poster run will repost MongoDB jobs with correct numbers.');
}

main().catch(err => {
  console.error('❌ Fatal:', err.message);
  process.exit(1);
});
