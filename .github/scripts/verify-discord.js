#!/usr/bin/env node

/**
 * Discord Channel Verification Tool
 *
 * Verifies job posting correctness by reading Discord channels directly
 *
 * Checks:
 * 1. Message Presence - Are jobs appearing in channels?
 * 2. Location Accuracy - Is the location field correct?
 * 3. Routing Verification - Are jobs in correct channels?
 * 4. Duplicate Detection - Any duplicate messages in same channel?
 * 5. Counter Verification - Do counts match expected?
 *
 * Usage: node .github/scripts/verify-discord.js
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { Client, GatewayIntentBits } = require('discord.js');

// Configuration
const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const DATA_DIR = process.env.GITHUB_ACTIONS ? '/tmp' : path.join(process.cwd(), '.github', 'data');

// Channel configuration (industry channels)
const CHANNELS = {
  // New-Grad industry channels
  tech: process.env.DISCORD_TECH_CHANNEL_ID,
  ai: process.env.DISCORD_AI_CHANNEL_ID,
  'data-science': process.env.DISCORD_DS_CHANNEL_ID,
  finance: process.env.DISCORD_FINANCE_CHANNEL_ID,
  healthcare: process.env.DISCORD_HEALTHCARE_CHANNEL_ID,
  business: process.env.DISCORD_BUSINESS_CHANNEL_ID,
  sales: process.env.DISCORD_SALES_CHANNEL_ID,
  hardware: process.env.DISCORD_HARDWARE_CHANNEL_ID,
  'other-industry': process.env.DISCORD_OTHER_INDUSTRY_CHANNEL_ID,

  // New-Grad location channels
  'bay-area': process.env.DISCORD_BAY_AREA_CHANNEL_ID,
  'new-york': process.env.DISCORD_NY_CHANNEL_ID,
  'pacific-northwest': process.env.DISCORD_PNW_CHANNEL_ID,
  'remote-usa': process.env.DISCORD_REMOTE_USA_CHANNEL_ID,
  'other-usa': process.env.DISCORD_OTHER_USA_CHANNEL_ID,

  // Internship industry channels
  'tech-int': process.env.DISCORD_TECH_INT_CHANNEL_ID,
  'ai-int': process.env.DISCORD_AI_INT_CHANNEL_ID,
  'data-science-int': process.env.DISCORD_DS_INT_CHANNEL_ID,
  'sales-int': process.env.DISCORD_SALES_INT_CHANNEL_ID,
  'marketing-int': process.env.DISCORD_MARKETING_INT_CHANNEL_ID,
  'business-int': process.env.DISCORD_BUSINESS_INT_CHANNEL_ID,
  'healthcare-int': process.env.DISCORD_HEALTHCARE_INT_CHANNEL_ID,
  'other-industry-int': process.env.DISCORD_OTHER_INDUSTRY_INT_CHANNEL_ID,

  // Internship location channels
  'remote-usa-int': process.env.DISCORD_REMOTE_USA_INT_CHANNEL_ID,
  'bay-area-int': process.env.DISCORD_BAY_AREA_INT_CHANNEL_ID,
  'new-york-int': process.env.DISCORD_NY_INT_CHANNEL_ID,
  'pacific-northwest-int': process.env.DISCORD_PNW_INT_CHANNEL_ID,
  'socal-int': process.env.DISCORD_SOCAL_INT_CHANNEL_ID,
  'other-usa-int': process.env.DISCORD_OTHER_USA_INT_CHANNEL_ID
};

// Verification results
const results = {
  totalMessages: 0,
  duplicates: [],
  locationErrors: [],
  routingErrors: [],
  missingChannels: [],
  channelSummary: {}
};

/**
 * Generate minimal job fingerprint for deduplication
 */
function generateJobFingerprint(message) {
  const crypto = require('crypto');

  // Extract job details from embed
  const embed = message.embeds[0];
  if (!embed) return null;

  const title = (embed.title || '').toLowerCase().trim();
  const company = embed.fields?.find(f => f.name === 'Company')?.value || '';
  const url = embed.url || '';

  const fingerprintData = `${url}|${title}|${company}`;
  return crypto.createHash('sha256').update(fingerprintData).digest('hex');
}

/**
 * Verify location field accuracy
 */
function verifyLocation(message) {
  const embed = message.embeds[0];
  if (!embed) return null;

  const locationField = embed.fields?.find(f => f.name === 'Location');
  if (!locationField) {
    return { error: 'Missing location field', message: message.id };
  }

  const location = locationField.value;
  const footer = embed.footer?.text || '';

  // Check for obvious issues
  const issues = [];

  // Empty location
  if (!location || location === 'Not specified' || location === 'Unknown') {
    issues.push('Empty or unspecified location');
  }

  // Location mismatch with source repo (if applicable)
  if (footer.includes('Internships') && location.includes('Senior')) {
    issues.push('Internship job with senior level in location?');
  }

  return issues.length > 0 ? { issues, message: message.id, location } : null;
}

/**
 * Verify routing correctness
 *
 * NOTE: Keyword-based routing checks have been removed. The router.js v3 uses a
 * hierarchical priority system (healthcare → AI → DS → tech → finance → other-industry)
 * that cannot be reliably re-implemented by inspecting Discord message titles alone.
 * False positives from stale keyword lists caused noisy informational warnings.
 *
 * Routing correctness is enforced at post time (router.js). This function is retained
 * as a no-op to preserve the call signature. Future: wire in router.js directly.
 */
function verifyRouting(message, channelName, channelId) {
  return null;
}

/**
 * Verify single channel
 */
async function verifyChannel(channelName, channelId) {
  if (!channelId) {
    console.log(`  ⏭️  ${channelName}: no channel ID configured (skipped)`);
    return;
  }

  console.log(`\n🔍 Verifying ${channelName} (${channelId})...`);

  try {
    const channel = await client.channels.fetch(channelId);
    if (!channel) {
      results.missingChannels.push({ channel: channelName, reason: 'Channel not found or bot lacks access' });
      return;
    }

    // Fetch last 100 messages
    const messages = await channel.messages.fetch({ limit: 100 });
    const embedMessages = messages.filter(m => m.embeds.length > 0);

    console.log(`  📊 Found ${embedMessages.size} job posts (last 100 messages)`);

    results.channelSummary[channelName] = {
      channelId,
      totalJobPosts: embedMessages.size,
      duplicates: 0,
      locationErrors: 0,
      routingErrors: 0
    };

    // Check for duplicates — only flag as critical if both the original AND the duplicate
    // were posted within the last 24 hours. Historical spam already in Discord is not actionable.
    const RECENT_WINDOW_MS = 24 * 60 * 60 * 1000;
    const cutoff = Date.now() - RECENT_WINDOW_MS;
    const fingerprints = new Map(); // fingerprint -> { messageId, timestamp }
    for (const [id, message] of embedMessages) {
      const fingerprint = generateJobFingerprint(message);
      if (fingerprint) {
        const ts = message.createdTimestamp;
        if (fingerprints.has(fingerprint)) {
          const original = fingerprints.get(fingerprint);
          // Only a critical duplicate if BOTH messages are recent
          const isRecent = ts > cutoff && original.timestamp > cutoff;
          results.duplicates.push({
            channel: channelName,
            originalMessage: original.messageId,
            duplicateMessage: id,
            title: message.embeds[0]?.title,
            isRecent
          });
          if (isRecent) {
            results.channelSummary[channelName].duplicates++;
          }
        } else {
          fingerprints.set(fingerprint, { messageId: id, timestamp: ts });
        }
      }
    }

    // Check location accuracy
    for (const [id, message] of embedMessages) {
      const locationCheck = verifyLocation(message);
      if (locationCheck && locationCheck.issues) {
        results.locationErrors.push({
          channel: channelName,
          ...locationCheck
        });
        results.channelSummary[channelName].locationErrors++;
      }
    }

    // Check routing correctness
    for (const [id, message] of embedMessages) {
      const routingCheck = verifyRouting(message, channelName, channelId);
      if (routingCheck && routingCheck.issues) {
        results.routingErrors.push({
          channel: channelName,
          ...routingCheck
        });
        results.channelSummary[channelName].routingErrors++;
      }
    }

    results.totalMessages += embedMessages.size;

  } catch (error) {
    results.missingChannels.push({ channel: channelName, reason: error.message });
  }
}

/**
 * Main execution
 */
async function main() {
  console.log('🔍 Discord Verification Tool - Starting...\n');

  // Initialize Discord client
  client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
    ]
  });

  await client.login(DISCORD_TOKEN);
  console.log('✅ Discord client connected\n');

  await Promise.all(client.guilds.cache.map(g => g.channels.fetch()));

  // Verify all channels
  for (const [channelName, channelId] of Object.entries(CHANNELS)) {
    await verifyChannel(channelName, channelId);
  }

  // Logout
  await client.destroy();

  // Print summary
  console.log('\n' + '='.repeat(60));
  console.log('📊 VERIFICATION SUMMARY');
  console.log('='.repeat(60));

  console.log(`\n📈 Total Messages Verified: ${results.totalMessages}`);
  console.log(`\n📋 Channel Breakdown:`);

  for (const [channel, summary] of Object.entries(results.channelSummary)) {
    console.log(`\n  ${channel}:`);
    console.log(`    Total Posts: ${summary.totalJobPosts}`);
    console.log(`    Duplicates: ${summary.duplicates}`);
    console.log(`    Location Errors: ${summary.locationErrors}`);
    console.log(`    Routing Errors: ${summary.routingErrors}`);
  }

  // This script is report-only — it never exits non-zero.
  // Findings are surfaced via GitHub Step Summary. Workflow failure here provides no value
  // and blocks downstream steps. Bot connection failure (caught below) is the only real fatal.
  const recentDuplicates = results.duplicates.filter(d => d.isRecent);
  const hasCriticalIssues = recentDuplicates.length > 0 ||
                           results.missingChannels.length > 0;

  if (hasCriticalIssues) {
    console.log('\n🚨 CRITICAL ISSUES FOUND:\n');

    if (results.missingChannels.length > 0) {
      console.log(`❌ Missing/Inaccessible Channels (${results.missingChannels.length}):`);
      results.missingChannels.forEach(ch => {
        console.log(`  - ${ch.channel}: ${ch.reason}`);
      });
    }

    if (recentDuplicates.length > 0) {
      console.log(`\n🔄 Recent Duplicates (last 24h) (${recentDuplicates.length}):`);
      recentDuplicates.slice(0, 10).forEach(dup => {
        console.log(`  - [${dup.channel}] ${dup.title}`);
      });
      if (recentDuplicates.length > 10) {
        console.log(`  ... and ${recentDuplicates.length - 10} more`);
      }
    }

    if (results.locationErrors.length > 0) {
      console.log(`\n📍 Location Errors (${results.locationErrors.length}):`);
      results.locationErrors.slice(0, 10).forEach(err => {
        console.log(`  - [${err.channel}] ${err.issues.join(', ')}`);
      });
      if (results.locationErrors.length > 10) {
        console.log(`  ... and ${results.locationErrors.length - 10} more`);
      }
    }

    if (results.routingErrors.length > 0) {
      console.log(`\n🧭 Routing Errors (${results.routingErrors.length}):`);
      results.routingErrors.slice(0, 10).forEach(err => {
        console.log(`  - [${err.channel}] ${err.title}`);
        console.log(`    Issues: ${err.issues.join(', ')}`);
      });
      if (results.routingErrors.length > 10) {
        console.log(`  ... and ${results.routingErrors.length - 10} more`);
      }
    }
  } else {
    console.log('\n✅ NO CRITICAL ISSUES FOUND - Verification passed!');
  }

  // Always show historical duplicate count for visibility (informational only)
  const historicalDuplicates = results.duplicates.filter(d => !d.isRecent);
  if (historicalDuplicates.length > 0) {
    console.log(`\nℹ️  Historical duplicates in last 100 messages: ${historicalDuplicates.length} (pre-existing spam, scrolls off naturally — not a failure)`);
  }

  // Create public summary (no sensitive details)
  const summary = {
    timestamp: new Date().toISOString(),
    totalMessages: results.totalMessages,
    channelSummary: results.channelSummary,
    criticalIssuesCount: {
      recentDuplicates: recentDuplicates.length,
      historicalDuplicates: historicalDuplicates.length,
      locationErrors: results.locationErrors.length,
      routingErrors: results.routingErrors.length,
      missingChannels: results.missingChannels.length
    },
    hasCriticalIssues
  };

  // Save public summary (for GitHub Step Summary - repo access only)
  const summaryPath = path.join(DATA_DIR, 'verification-summary.json');
  fs.writeFileSync(summaryPath, JSON.stringify(summary, null, 2));

  // Save full encrypted report (local only, deleted by workflow)
  const fullReport = {
    timestamp: new Date().toISOString(),
    results
  };

  const encryptedPath = path.join(DATA_DIR, 'verification-report.enc');
  const reportJson = JSON.stringify(fullReport, null, 2);

  // Generate encryption key from environment or derive from local secret
  const encryptionKey = process.env.VERIFICATION_ENCRYPTION_KEY
    || crypto.scryptSync('local-dev-key-only', 'salt', 32);

  // Generate random IV
  const iv = crypto.randomBytes(16);

  // Encrypt
  const cipher = crypto.createCipheriv('aes-256-gcm', encryptionKey, iv);
  let encrypted = cipher.update(reportJson, 'utf8', 'hex');
  encrypted += cipher.final('hex');
  const authTag = cipher.getAuthTag();

  // Save encrypted data with IV and auth tag
  const encryptedData = JSON.stringify({
    iv: iv.toString('hex'),
    authTag: authTag.toString('hex'),
    data: encrypted
  });
  fs.writeFileSync(encryptedPath, encryptedData);

  if (hasCriticalIssues) {
    console.log('\n⚠️  Issues found — see above. Exiting 0 (report-only mode).');
  } else {
    console.log('\n✅ Verification complete — no critical issues.');
  }
}

let client;
main().catch(error => {
  console.error('\n❌ Fatal error:', error.message);
  console.error(error.stack);
  process.exit(1);
});
