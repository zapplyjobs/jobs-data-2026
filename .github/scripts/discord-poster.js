#!/usr/bin/env node

/**
 * Discord Poster - Aggregator Script
 *
 * Reads all_jobs.json directly from local .github/data/ and posts to Discord.
 * Option A: Single local file read, no inter-repo HTTP calls.
 */

const fs = require('fs');
const path = require('path');

// Import modules
const Router = require('./src/routing/router');
const Location = require('./src/routing/location');
// Two-layer dedup — intentional TTL difference:
//   PostedJobsManager (7-day): matches Discord scroll window — prevents re-posting jobs still visible
//   GlobalDedupeManager (7-day): matches staleness gate — prevents re-posting jobs within active window
const PostedJobsManager = require('./src/data/posted-jobs-manager-v2');
const GlobalDedupeManager = require('./src/data/global-dedupe-manager');
const {
  LOCATION_CHANNEL_CONFIG, CHANNEL_CONFIG,
  INTERNSHIP_CHANNEL_CONFIG, INTERNSHIP_LOCATION_CHANNEL_CONFIG
} = require('./src/discord/config');

// Load company data for emoji and tier detection
const companies = JSON.parse(fs.readFileSync(path.join(__dirname, 'companies.json'), 'utf8'));

const DISCORD_TOKEN = process.env.DISCORD_TOKEN;

// Data directory
const DATA_DIR = path.join(process.cwd(), '.github', 'data');

/**
 * Read all_jobs.json (JSONL format) and normalize field names to match
 * the job_* schema expected by router, location, and posting functions.
 */
function loadAllJobs() {
  const filePath = path.join(DATA_DIR, 'all_jobs.json');
  const raw = fs.readFileSync(filePath, 'utf8');
  return raw.trim().split('\n').map(line => {
    const j = JSON.parse(line);
    return {
      ...j,
      job_title: j.title,
      job_description: j.description,
      employer_name: j.company_name,
      job_apply_link: j.apply_url || j.url,
      job_posted_at_datetime_utc: j.posted_at,
      job_is_remote: j.workplace_type?.toLowerCase() === 'remote' || j.tags?.locations?.includes('remote') || false,
      _sourceRepo: j.id?.split('-')[0] || 'aggregator',
    };
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
    // Languages
    'python': 'Python', 'java ': 'Java', 'javascript': 'JavaScript', 'typescript': 'TypeScript',
    'c++': 'C++', 'c#': 'C#', 'go ': 'Go', 'golang': 'Go', 'rust ': 'Rust',
    'ruby': 'Ruby', 'php': 'PHP', 'swift': 'Swift', 'kotlin': 'Kotlin',
    'scala': 'Scala', 'r ': 'R', 'matlab': 'MATLAB', 'sql': 'SQL',
    // Domains
    'machine learning': 'ML', 'deep learning': 'Deep Learning', 'ai': 'AI',
    'data science': 'Data Science', 'data engineer': 'Data Engineering',
    'digital engineer': 'Software Engineer', 'digital engineering': 'Software Engineer',
    // Platforms
    'aws': 'AWS', 'azure': 'Azure', 'gcp': 'GCP', 'cloud': 'Cloud',
    // Frameworks
    'react': 'React', 'angular': 'Angular', 'vue': 'Vue', 'node': 'Node.js',
    'spring': 'Spring', 'django': 'Django', 'flask': 'Flask',
    'kubernetes': 'Kubernetes', 'docker': 'Docker', 'terraform': 'Terraform',
    // Specialties
    'ios': 'iOS', 'android': 'Android', 'mobile': 'Mobile',
    'frontend': 'Frontend', 'backend': 'Backend', 'fullstack': 'Full Stack',
    'devops': 'DevOps', 'security': 'Security', 'blockchain': 'Blockchain',
    'embedded': 'Embedded', 'fpga': 'FPGA', 'iot': 'IoT',
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
  if (!tags.includes('Data Science') && (title.includes('data scientist') || title.includes('analyst'))) {
    tags.push('Data Science');
  }
  if (!tags.includes('ML') && (title.includes('machine learning') || title.includes('ml engineer'))) {
    tags.push('ML');
  }
  if (title.includes('product manager') || title.includes('pm ')) {
    tags.push('Product Manager');
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
  const url = job.job_apply_link || job.url || '';
  const title = (job.job_title || '').toLowerCase().trim();
  const company = (job.employer_name || '').toLowerCase().trim();

  // Create fingerprint from URL + title + company
  const fingerprintData = `${url}|${title}|${company}`;
  return crypto.createHash('sha256').update(fingerprintData).digest('hex');
}

/**
 * Post single job to Discord channel
 */
async function postJobToDiscord(job, channelId, discordClient, channelName, channelJobNumber, enrichedMap = new Map()) {
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
    .setURL(job.job_apply_link || '#')
    .setColor(0x00A8E8)
    .addFields(
      { name: '🏢 Company', value: job.employer_name || 'Not specified', inline: true },
      { name: '📍 Location', value: formatLocationWithAbbr(job), inline: true },
      { name: '💰 Posted', value: formatPostedDate(job), inline: true }
    );

  // Add tags field — merge enriched skills + synthetic tags, all as #Capitalized
  const enriched = enrichedMap.get(job.id);
  const enrichedSkills = enriched?.required_skills?.slice(0, 8) || [];
  const allTags = [];

  // Enriched skills first (higher quality — extracted from description)
  for (const skill of enrichedSkills) {
    const capitalized = skill.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(' ');
    allTags.push(capitalized);
  }

  // Then synthetic tags (from title/company), skip duplicates
  for (const tag of tags) {
    const lower = tag.toLowerCase();
    if (!allTags.some(t => t.toLowerCase() === lower)) {
      allTags.push(tag);
    }
  }

  if (allTags.length > 0) {
    embed.addFields({
      name: '🏷️ Tags',
      value: allTags.slice(0, 8).map(t => `#${t}`).join(' '),
      inline: false
    });
  }

  // Visa sponsorship tag — tiered to match GitHub README labels
  // Sponsors Visa = hard text match (sponsors_visa) or ATS form question (visa_question_present)
  // 🏢 Sponsor Employer = LCA database match only (not available in enriched_jobs.json yet)
  if (enriched) {
    const hasHard = enriched.sponsors_visa === true || enriched.visa_question_present === true;
    if (hasHard) {
      embed.addFields({
        name: '🌐 Visa',
        value: 'Sponsors Visa',
        inline: true
      });
    }
  }

  // Add footer with job number and channel name
  if (channelName && channelJobNumber) {
    embed.setFooter({
      text: `Job #${channelJobNumber} in #${channelName} | Jobs by zapply.jobs`
    });
  } else {
    embed.setFooter({
      text: (job._sourceRepo || 'aggregator') + ' | Jobs by zapply.jobs'
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

  // Load jobs from local all_jobs.json (Option A)
  console.log('\n📂 Loading jobs from all_jobs.json...');
  const allJobs = loadAllJobs();
  console.log(`✅ Loaded ${allJobs.length} jobs`);

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

  // UX-1: Sort newest-first so Discord channels show most recent jobs first
  uniqueJobs.sort((a, b) => {
    const aDate = a.job_posted_at_datetime_utc ? new Date(a.job_posted_at_datetime_utc).getTime() : 0;
    const bDate = b.job_posted_at_datetime_utc ? new Date(b.job_posted_at_datetime_utc).getTime() : 0;
    return bDate - aDate;
  });

  // UX-3: Title dedup — cap 1 post per title+company per run (OUT-32).
  // Prevents multi-location spam: Lowe's "Part Time - Fulfillment Associate" posted
  // 129 times across different store locations. Each has a unique jobId and URL, so
  // fingerprint dedup misses them. Only the newest posting per title+company is kept.
  const MAX_SAME_TITLE_PER_COMPANY = 1;
  const titleCompanySeen = new Map();
  const titleDedupedJobs = uniqueJobs.filter(job => {
    const key = `${(job.job_title || '').toLowerCase().trim()}|${(job.employer_name || '').toLowerCase().trim()}`;
    const count = titleCompanySeen.get(key) || 0;
    if (count >= MAX_SAME_TITLE_PER_COMPANY) {
      return false;
    }
    titleCompanySeen.set(key, count + 1);
    return true;
  });
  const titleDedupedCount = uniqueJobs.length - titleDedupedJobs.length;
  if (titleDedupedCount > 0) {
    console.log(`✅ Title dedup: removed ${titleDedupedCount} multi-location duplicates (${titleDedupedJobs.length} unique title+company)`);
  }
  uniqueJobs.length = 0;
  uniqueJobs.push(...titleDedupedJobs);

  // UX-2: Load enriched data for visa tag lookup (id → visa_question_present)
  const enrichedMap = new Map();
  const enrichedPath = path.join(DATA_DIR, 'enriched_jobs.json');
  if (fs.existsSync(enrichedPath)) {
    const enrichedLines = fs.readFileSync(enrichedPath, 'utf8').trim().split('\n').filter(Boolean);
    for (const line of enrichedLines) {
      try {
        const e = JSON.parse(line);
        if (e.id) enrichedMap.set(e.id, e);
      } catch (_) {}
    }
    console.log(`✅ Loaded ${enrichedMap.size} enriched records for visa tagging`);
  }

  // Post jobs
  console.log('\n📤 Posting jobs to Discord...');
  const MAX_POSTS_PER_RUN = parseInt(process.env.MAX_POSTS_PER_RUN) || 20;
  let postedCount = 0;
  let skippedCount = 0;
  let filteredCount = 0;
  let nonUsCount = 0;
  let staleCount = 0;
  let midLevelCount = 0;

  for (const job of uniqueJobs) {
    try {
      // Check by stable jobId first (prevents re-posting when url/title/company vary across runs)
      if (globalDedupeManager.hasJobIdBeenPosted && globalDedupeManager.hasJobIdBeenPosted(job.id)) {
        console.log(`  ⏭️  Skipping (jobId already posted): ${job.job_title} @ ${job.employer_name}`);
        skippedCount++;
        continue;
      }

      // Generate fingerprint for this job
      const fingerprint = generateMinimalJobFingerprint(job);

      // Check if already posted globally (across all runs, 7-day TTL)
      if (globalDedupeManager.hasBeenPosted(fingerprint)) {
        console.log(`  ⏭️  Skipping (already posted globally): ${job.job_title} @ ${job.employer_name}`);
        skippedCount++;
        continue;
      }

      // Check if already posted locally (this run's database)
      const localJobId = postedJobsManager.generateJobId(job);
      if (postedJobsManager.hasBeenPosted(localJobId, job)) {
        skippedCount++;
        continue;
      }

      // Skip non-US jobs (settled rule: no us tag = don't post)
      if (!job.tags?.locations?.includes('us')) {
        nonUsCount++;
        continue;
      }

      // Skip mid-level jobs — new-grad and internship boards only (DISCORD-MID-1)
      if (job.tags?.employment === 'mid_level') {
        midLevelCount++;
        continue;
      }

      // Skip jobs older than 7 days (prevents stale ATS listings from posting).
      // Jobs with no posted_at date are treated as stale (age = Infinity) — a missing
      // date is not evidence of freshness, and posting undated jobs causes noise.
      const jobAge = job.job_posted_at_datetime_utc
        ? (Date.now() - new Date(job.job_posted_at_datetime_utc).getTime()) / (1000 * 60 * 60 * 24)
        : Infinity;
      if (jobAge > 7) {
        staleCount++;
        continue;
      }

      // Determine board type: internship vs new-grad
      const isInternship = job.tags?.employment === 'internship';
      const activeCHANNEL_CONFIG = isInternship ? INTERNSHIP_CHANNEL_CONFIG : CHANNEL_CONFIG;
      const activeLOCATION_CONFIG = isInternship ? INTERNSHIP_LOCATION_CHANNEL_CONFIG : LOCATION_CHANNEL_CONFIG;

      // For internships: check sales/marketing before generic router (router removed these channels)
      let industryRouting;
      if (isInternship) {
        const title = (job.job_title || '').toLowerCase();
        if (/\b(sales|account executive|business development)\b/.test(title) && activeCHANNEL_CONFIG.sales) {
          industryRouting = { channelId: activeCHANNEL_CONFIG.sales, category: 'sales', matchType: 'internship-sales' };
        } else if (/\b(marketing|growth|brand|content|social media|seo|communications)\b/.test(title) && activeCHANNEL_CONFIG.marketing) {
          industryRouting = { channelId: activeCHANNEL_CONFIG.marketing, category: 'marketing', matchType: 'internship-marketing' };
        } else {
          industryRouting = Router.getJobChannelDetails(job, activeCHANNEL_CONFIG);
        }
      } else {
        // Route job to channels (get both industry and location channels)
        industryRouting = Router.getJobChannelDetails(job, activeCHANNEL_CONFIG);
      }
      const locationChannelId = Location.getJobLocationChannelWithConfig(job, activeLOCATION_CONFIG);

      const channelsToPost = [];

      // Add industry channel
      if (industryRouting && industryRouting.channelId) {
        channelsToPost.push({
          channelId: industryRouting.channelId,
          category: industryRouting.category,
          type: 'industry'
        });
      } else if (industryRouting && industryRouting.category === 'filtered') {
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

        // Get channel name for footer (number assigned after successful post)
        let channelName = null;
        try {
          const channelObj = await discordClient.channels.fetch(channelInfo.channelId);
          channelName = channelObj?.name || null;
        } catch (e) {
          // Channel name lookup failed, continue without it
        }

        // Assign job number AFTER successful post to prevent counter inflation on failures
        const channelJobNumber = postedJobsManager.peekNextChannelJobNumber(channelInfo.channelId);

        const message = await postJobToDiscord(job, channelInfo.channelId, discordClient, channelName, channelJobNumber, enrichedMap);

        // Post succeeded — now commit the counter increment
        postedJobsManager.commitChannelJobNumber(channelInfo.channelId);

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
          job.id,
          job._sourceRepo,
          channelInfo.channelId,
          message.id
        );
      }

      postedCount++;
      console.log(`  ✅ Posted: ${job.job_title} @ ${job.employer_name}`);
      if (postedCount >= MAX_POSTS_PER_RUN) {
        console.log(`\n⏸️  Reached per-run limit (${MAX_POSTS_PER_RUN}). Remaining jobs deferred to next run.`);
        break;
      }
    } catch (error) {
      console.error(`  ❌ Error posting ${job.job_title}: ${error.message}`);
    }
  }

  console.log(`\n📊 Posting Summary:`);
  console.log(`  ✅ Posted: ${postedCount} jobs`);
  console.log(`  ⏭️  Skipped (already posted): ${skippedCount} jobs`);
  console.log(`  🏷️  Title dedup (multi-location): ${titleDedupedCount} jobs`);
  console.log(`  🌍 Filtered (non-US): ${nonUsCount} jobs`);
  console.log(`  📅 Filtered (too old): ${staleCount} jobs`);
  console.log(`  🔵 Filtered (mid-level): ${midLevelCount} jobs`);
  console.log(`  🚫 Filtered (no channel matched): ${filteredCount} jobs`);

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
