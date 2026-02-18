#!/usr/bin/env node

/**
 * Discord Poster - Aggregator Script
 *
 * Fetches jobs from all repos and posts to Discord
 * Implements global deduplication across repos
 */

const https = require('https');
const fs = require('fs');
const path = require('path');

// Import modules
const Router = require('./src/routing/router');
const Location = require('./src/routing/location');
const PostedJobsManager = require('./src/data/posted-jobs-manager-v2');
const GlobalDedupeManager = require('./src/data/global-dedupe-manager');
const { LOCATION_CHANNEL_CONFIG, CHANNEL_CONFIG } = require('./src/discord/config');

// Load company data for emoji and tier detection
const companies = JSON.parse(fs.readFileSync(path.join(__dirname, 'companies.json'), 'utf8'));

// GitHub token for API requests
const GITHUB_TOKEN = process.env.GITHUB_TOKEN;
const DISCORD_TOKEN = process.env.DISCORD_TOKEN;

// Repositories to fetch jobs from
const REPOS = [
  { owner: 'zapplyjobs', repo: 'New-Grad-Jobs-2026', name: 'New-Grad' },
  { owner: 'zapplyjobs', repo: 'Internships-2026', name: 'Internships' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Data-Science-Jobs-2026', name: 'Data-Science' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Hardware-Engineering-Jobs-2026', name: 'Hardware' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Nursing-Jobs-2026', name: 'Nursing' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Software-Engineering-Jobs-2026', name: 'Software-Engineering' }
];

// Data directory
const DATA_DIR = path.join(process.cwd(), '.github', 'data');

/**
 * Fetch file content from GitHub (raw URL)
 * Uses raw.githubusercontent.com instead of Contents API to avoid 1MB limit
 */
function fetchGitHubFile(owner, repo, filePath) {
  return new Promise((resolve, reject) => {
    // Use raw.githubusercontent.com instead of Contents API (no size limit)
    const url = `https://raw.githubusercontent.com/${owner}/${repo}/main/${filePath}`;

    https.get(url, {
      headers: {
        'User-Agent': 'Zapply-Aggregator',
        'Authorization': `Bearer ${GITHUB_TOKEN}`, // Still needed for private repos
      }
    }, (res) => {
      // Handle 404 gracefully
      if (res.statusCode === 404) {
        resolve(null);
        return;
      }

      // Handle other errors
      if (res.statusCode !== 200) {
        reject(new Error(`HTTP ${res.statusCode} for ${owner}/${repo}/${filePath}`));
        return;
      }

      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(data));
    }).on('error', reject);
  });
}

/**
 * Format job location for display
 */
function formatLocation(job) {
  const city = job.job_city || '';
  const state = job.job_state || '';
  const isRemote = job.job_is_remote || false;

  if (isRemote) {
    return 'Remote';
  }

  if (city && state) {
    return `${city}, ${state}`;
  } else if (city) {
    return city;
  } else if (state) {
    return state;
  }

  return 'Not specified';
}

// State name to abbreviation mapping for consistent location formatting
const STATE_ABBREVIATIONS = {
  'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
  'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'florida': 'FL', 'georgia': 'GA',
  'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
  'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
  'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS', 'missouri': 'MO',
  'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ',
  'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH',
  'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
  'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT',
  'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
  'district of columbia': 'DC'
};

/**
 * Format location for consistent display (with state abbreviation)
 */
function formatLocationWithAbbr(job) {
  const city = job.job_city || '';
  const state = job.job_state || '';
  const isRemote = job.job_is_remote || false;

  if (isRemote || (city && city.toLowerCase() === 'remote')) {
    return 'Remote';
  }

  // Convert state to abbreviation if it's a full state name
  let stateAbbr = state;
  if (state) {
    const stateLower = state.toLowerCase().trim();
    stateAbbr = STATE_ABBREVIATIONS[stateLower] || state;
  }

  // If no city but has state, just show state
  if (!city || city.trim() === '' || city.toLowerCase() === 'not specified') {
    return stateAbbr || 'Remote';
  }

  // If has city and state, show "City, ST"
  if (stateAbbr) {
    return `${city}, ${stateAbbr}`;
  }

  // If only city, show city
  return city;
}

/**
 * Generate tags for a job based on title, description, and company
 */
function generateTags(job) {
  const tags = [];
  const title = (job.job_title || '').toLowerCase();
  const description = (job.job_description || '').toLowerCase();
  const company = job.employer_name || '';

  // Location tags - ONLY tag as Remote if location field explicitly says remote
  if (job.job_city && job.job_city.toLowerCase().includes('remote')) {
    tags.push('Remote');
  }

  // Add major city tags
  const majorCities = {
    'san francisco': 'SF', 'sf': 'SF', 'bay area': 'SF',
    'new york': 'NYC', 'nyc': 'NYC', 'manhattan': 'NYC',
    'seattle': 'Seattle', 'bellevue': 'Seattle', 'redmond': 'Seattle',
    'austin': 'Austin', 'los angeles': 'LA', 'la': 'LA',
    'boston': 'Boston', 'chicago': 'Chicago', 'denver': 'Denver'
  };

  const cityKey = (job.job_city || '').toLowerCase();
  if (majorCities[cityKey]) {
    tags.push(majorCities[cityKey]);
  }

  // Company tier tags
  if (companies.faang_plus.some(c => c.name === company)) {
    tags.push('FAANG');
  } else if (companies.unicorn_startups.some(c => c.name === company)) {
    tags.push('Unicorn');
  } else if (companies.fintech.some(c => c.name === company)) {
    tags.push('Fintech');
  } else if (companies.gaming.some(c => c.name === company)) {
    tags.push('Gaming');
  }

  // Technology/skill tags (limit to most relevant - check title first)
  const techStack = {
    'machine learning': 'ML', 'ai': 'AI', 'data science': 'DataScience',
    'digital engineer': 'SWE', 'digital engineering': 'SWE',
    'ios': 'iOS', 'android': 'Android', 'mobile': 'Mobile',
    'frontend': 'Frontend', 'backend': 'Backend', 'fullstack': 'FullStack',
    'devops': 'DevOps', 'security': 'Security', 'blockchain': 'Blockchain',
    'aws': 'AWS', 'azure': 'Azure', 'gcp': 'GCP'
  };

  // Only match tags from title (more accurate than description)
  for (const [keyword, tag] of Object.entries(techStack)) {
    if (title.includes(keyword)) {
      tags.push(tag);
    }
  }

  // Limit to max 8 tags for consistency
  if (tags.length > 8) {
    tags.length = 8;
  }

  // Role category tags (only if not already added via tech stack)
  if (!tags.includes('DataScience') && (title.includes('data scientist') || title.includes('analyst'))) {
    tags.push('DataScience');
  }
  if (!tags.includes('ML') && (title.includes('machine learning') || title.includes('ml engineer'))) {
    tags.push('ML');
  }
  if (title.includes('product manager') || title.includes('pm ')) {
    tags.push('ProductManager');
  }
  if (title.includes('designer') || title.includes('ux') || title.includes('ui')) {
    tags.push('Design');
  }

  return [...new Set(tags)]; // Remove duplicates
}

/**
 * Format posted date for display
 */
function formatPostedDate(job) {
  const now = new Date();
  const companyDate = job.job_posted_at_datetime_utc ? new Date(job.job_posted_at_datetime_utc) : null;

  if (companyDate) {
    // Show both Discord and Company dates
    const discordDateStr = now.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric'
    });
    const companyDateStr = companyDate.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric'
    });
    return `Discord: ${discordDateStr}\nCompany: ${companyDateStr}`;
  }

  // Fallback for jobs without company date
  return now.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric'
  });
}

/**
 * Generate minimal job fingerprint for deduplication
 */
function generateMinimalJobFingerprint(job) {
  const crypto = require('crypto');

  // Use URL as primary key (most unique identifier)
  const url = job.job_apply_link || job.job_google_link || job.url || '';
  const title = (job.job_title || '').toLowerCase().trim();
  const company = (job.employer_name || '').toLowerCase().trim();

  // Create fingerprint from URL + title + company
  const fingerprintData = `${url}|${title}|${company}`;
  return crypto.createHash('sha256').update(fingerprintData).digest('hex');
}

/**
 * Post single job to Discord channel
 */
async function postJobToDiscord(job, channelId, discordClient, channelName, channelJobNumber) {
  const channel = await discordClient.channels.fetch(channelId);
  if (!channel) {
    throw new Error(`Channel not found: ${channelId}`);
  }

  // Generate tags and find company emoji
  const tags = generateTags(job);
  const company = companies.faang_plus.find(c => c.name === job.employer_name) ||
                  companies.unicorn_startups.find(c => c.name === job.employer_name) ||
                  companies.fintech.find(c => c.name === job.employer_name) ||
                  companies.gaming.find(c => c.name === job.employer_name) ||
                  companies.top_tech.find(c => c.name === job.employer_name) ||
                  companies.enterprise_saas.find(c => c.name === job.employer_name);

  // Build embed with proper format
  const { EmbedBuilder } = require('discord.js');

  const embed = new EmbedBuilder()
    .setTitle(job.job_title || 'Untitled Position')
    .setURL(job.job_apply_link || job.job_google_link || '#')
    .setColor(0x00A8E8) // Match New-Grad color
    .addFields(
      { name: '🏢 Company', value: job.employer_name || 'Not specified', inline: true },
      { name: '📍 Location', value: formatLocationWithAbbr(job), inline: true },
      { name: '💰 Posted', value: formatPostedDate(job), inline: true }
    );

  // Add tags field with hashtag formatting
  if (tags.length > 0) {
    embed.addFields({
      name: '🏷️ Tags',
      value: tags.map(tag => `#${tag}`).join(' '),
      inline: false
    });
  }

  // Add footer with job number and channel name
  if (channelName && channelJobNumber) {
    embed.setFooter({
      text: `Job #${channelJobNumber} in #${channelName}`
    });
  } else {
    // Fallback footer if no channel info
    embed.setFooter({
      text: job._sourceRepo || 'Unknown'
    });
  }

  const message = await channel.send({ embeds: [embed] });
  return message;
}

/**
 * Main execution
 */
async function main() {
  console.log('🚀 Aggregator Discord Poster - Starting...');

  // Initialize Discord client
  const { Client, GatewayIntentBits } = require('discord.js');
  const discordClient = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages
    ]
  });

  await discordClient.login(DISCORD_TOKEN);
  console.log('✅ Discord client connected');

  // Initialize managers
  const postedJobsManager = new PostedJobsManager();
  const globalDedupeManager = new GlobalDedupeManager();

  // Fetch jobs from all repos
  console.log('\n📡 Fetching jobs from repos...');
  const allJobs = [];

  for (const repo of REPOS) {
    console.log(`  📥 ${repo.name}...`);

    try {
      const jobsData = await fetchGitHubFile(repo.owner, repo.repo, '.github/data/current_jobs.json');

      if (jobsData) {
        const jobs = JSON.parse(jobsData);
        // Add source repo to each job
        jobs.forEach(job => {
          job._sourceRepo = repo.name;
        });
        allJobs.push(...jobs);
        console.log(`    ✅ Got ${jobs.length} jobs`);
      } else {
        console.log(`    ⚠️  No current_jobs.json found`);
      }
    } catch (error) {
      console.error(`    ❌ Error: ${error.message}`);
    }
  }

  console.log(`\n📊 Total jobs fetched: ${allJobs.length}`);

  // Global deduplication (in-memory for current batch)
  console.log('\n🔄 Deduplicating jobs within batch...');
  const seenFingerprints = new Set();
  const uniqueJobs = allJobs.filter(job => {
    const fingerprint = generateMinimalJobFingerprint(job);
    if (seenFingerprints.has(fingerprint)) {
      console.log(`  ⏭️  Skipping batch duplicate: ${job.job_title} @ ${job.employer_name}`);
      return false;
    }
    seenFingerprints.add(fingerprint);
    return true;
  });

  console.log(`✅ After batch deduplication: ${uniqueJobs.length} jobs`);

  // Get channels from environment
  const channels = {
    // New-Grad channels
    tech: process.env.DISCORD_TECH_CHANNEL_ID,
    ai: process.env.DISCORD_AI_CHANNEL_ID,
    ds: process.env.DISCORD_DS_CHANNEL_ID,
    finance: process.env.DISCORD_FINANCE_CHANNEL_ID,
    bayArea: process.env.DISCORD_BAY_AREA_CHANNEL_ID,
    ny: process.env.DISCORD_NY_CHANNEL_ID,
    pnw: process.env.DISCORD_PNW_CHANNEL_ID,
    remoteUsa: process.env.DISCORD_REMOTE_USA_CHANNEL_ID,
    otherUsa: process.env.DISCORD_OTHER_USA_CHANNEL_ID,

    // Internships channels
    sales: process.env.DISCORD_SALES_CHANNEL_ID,
    marketing: process.env.DISCORD_MARKETING_CHANNEL_ID,
    other: process.env.DISCORD_OTHER_CHANNEL_ID,
    bayAreaInt: process.env.DISCORD_BAY_AREA_INT_CHANNEL_ID,
    socalInt: process.env.DISCORD_SOCAL_INT_CHANNEL_ID
  };

  // Post jobs
  console.log('\n📤 Posting jobs to Discord...');
  let postedCount = 0;
  let skippedCount = 0;
  let filteredCount = 0;

  for (const job of uniqueJobs) {
    try {
      // Generate fingerprint for this job
      const fingerprint = generateMinimalJobFingerprint(job);

      // Check if already posted globally (across all runs, 14-day TTL)
      if (globalDedupeManager.hasBeenPosted(fingerprint)) {
        console.log(`  ⏭️  Skipping (already posted globally): ${job.job_title} @ ${job.employer_name}`);
        skippedCount++;
        continue;
      }

      // Check if already posted locally (this run's database)
      if (postedJobsManager.hasBeenPosted(job)) {
        skippedCount++;
        continue;
      }

      // Route job to channels (get both industry and location channels)
      const industryRouting = Router.getJobChannelDetails(job, CHANNEL_CONFIG);
      const locationChannelId = Location.getJobLocationChannel(job);

      const channelsToPost = [];

      // Add industry channel
      if (industryRouting && industryRouting.channelId) {
        channelsToPost.push({
          channelId: industryRouting.channelId,
          category: industryRouting.category,
          type: 'industry'
        });
      } else if (industryRouting && industryRouting.category === 'filtered') {
        // Job was filtered out (doesn't match any active channels)
        console.log(`  🚫 Filtered: ${job.job_title} @ ${job.employer_name} (${industryRouting.reason})`);
        filteredCount++;
        continue;
      }

      // Add location channel (if applicable)
      if (locationChannelId) {
        channelsToPost.push({
          channelId: locationChannelId,
          category: industryRouting?.category || 'tech',
          type: 'location'
        });
      }

      // Post to each channel
      for (const channelInfo of channelsToPost) {
        const envVarName = Object.keys(process.env).find(key => process.env[key] === channelInfo.channelId);

        if (!envVarName) {
          console.log(`  ⚠️  Channel ID ${channelInfo.channelId} not found in environment`);
          continue;
        }

        // Get channel name and job number for footer
        let channelName = null;
        try {
          const channelObj = await discordClient.channels.fetch(channelInfo.channelId);
          channelName = channelObj?.name || null;
        } catch (e) {
          // Channel name lookup failed, continue without it
        }

        const channelJobNumber = postedJobsManager.getChannelJobNumber(channelInfo.channelId);

        const message = await postJobToDiscord(job, channelInfo.channelId, discordClient, channelName, channelJobNumber);

        // Track posting in local manager
        postedJobsManager.markAsPostedToChannel(
          job,
          message.id,
          channelInfo.channelId,
          channelInfo.category,
          channelJobNumber
        );

        // Track posting in global dedupe store
        const fingerprint = generateMinimalJobFingerprint(job);
        globalDedupeManager.markAsPosted(
          fingerprint,
          job.job_id || job.id,
          job._sourceRepo,
          channelInfo.channelId,
          message.id
        );
      }

      postedCount++;
      console.log(`  ✅ Posted: ${job.job_title} @ ${job.employer_name}`);
    } catch (error) {
      console.error(`  ❌ Error posting ${job.job_title}: ${error.message}`);
    }
  }

  console.log(`\n📊 Posting Summary:`);
  console.log(`  ✅ Posted: ${postedCount} jobs`);
  console.log(`  ⏭️  Skipped (already posted): ${skippedCount} jobs`);
  console.log(`  🚫 Filtered (non-tech roles): ${filteredCount} jobs`);

  // Save databases
  console.log('\n💾 Saving databases...');
  postedJobsManager.savePostedJobs();
  globalDedupeManager.saveStore();
  console.log('✅ Databases saved');

  // Logout
  await discordClient.destroy();

  console.log('\n✅ Aggregator run complete!');
}

main().catch(error => {
  console.error('\n❌ Fatal error:', error.message);
  console.error(error.stack);
  process.exit(1);
});
