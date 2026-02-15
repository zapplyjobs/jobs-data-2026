/**
 * Senior Job Filter
 *
 * Filters out senior-level jobs to focus on entry-level and new-grad positions.
 * Based on Phase 1 architecture requirements.
 *
 * Filtering criteria:
 * 1. Experience level: >5 years experience required
 * 2. Job titles: Contains senior-level keywords
 * 3. Conservative approach: When in doubt, include the job
 */

/**
 * Senior job title keywords
 * These indicate a senior-level position
 */
const SENIOR_KEYWORDS = [
    'senior',
    'sr.',
    'sr ',
    'lead',
    'principal',
    'staff',
    'manager',
    'director',
    'vp',
    'vice president',
    'chief',
    'cto',
    'cio',
    'ceo',
    'head of',
    'architect'  // Solution Architect, Enterprise Architect typically senior
];

/**
 * Experience patterns indicating senior level
 * Regex patterns for detecting years of experience
 */
const SENIOR_EXPERIENCE_PATTERNS = [
    /(\d+)-(\d+)\s*years/i,        // "5-7 years", "7-10 years" - CHECK THIS FIRST (range)
    /(\d+)\+?\s*years/i,           // "5+ years", "7 years"
    /minimum\s+(\d+)\s+years/i,    // "minimum 5 years"
    /at least\s+(\d+)\s+years/i,   // "at least 5 years"
    /(\d+)\s*yrs/i                 // "5yrs", "7 yrs"
];

const MIN_SENIOR_YEARS = 5; // Jobs requiring 5+ years are considered senior

/**
 * Check if job title contains senior-level keywords
 * @param {string} title - Job title
 * @returns {boolean} - True if title indicates senior level
 */
function hasSeniorTitle(title) {
    if (!title || typeof title !== 'string') {
        return false;
    }

    const lowerTitle = title.toLowerCase();

    // Check for senior keywords
    for (const keyword of SENIOR_KEYWORDS) {
        if (lowerTitle.includes(keyword)) {
            return true;
        }
    }

    return false;
}

/**
 * Extract years of experience from text
 * @param {string} text - Text to analyze (job description, title, etc.)
 * @returns {number|null} - Years of experience required, or null if not found
 */
function extractYearsOfExperience(text) {
    if (!text || typeof text !== 'string') {
        return null;
    }

    for (const pattern of SENIOR_EXPERIENCE_PATTERNS) {
        const match = text.match(pattern);
        if (match) {
            // For range patterns (e.g., "5-7 years"), use the minimum
            if (match[2]) {
                return parseInt(match[1], 10);
            }
            // For single number patterns
            return parseInt(match[1], 10);
        }
    }

    return null;
}

/**
 * Check if job requires senior-level experience
 * @param {Object} job - Job object
 * @returns {boolean} - True if job requires 5+ years experience
 */
function requiresSeniorExperience(job) {
    // Check title for experience indicators
    const titleYears = extractYearsOfExperience(job.title || '');
    if (titleYears !== null && titleYears >= MIN_SENIOR_YEARS) {
        return true;
    }

    // Check description for experience indicators
    const descYears = extractYearsOfExperience(job.description || '');
    if (descYears !== null && descYears >= MIN_SENIOR_YEARS) {
        return true;
    }

    return false;
}

/**
 * Determine if job is senior-level
 * @param {Object} job - Job object to check
 * @returns {boolean} - True if job is senior-level (should be filtered)
 */
function isSeniorJob(job) {
    // Check 1: Senior title keywords
    if (hasSeniorTitle(job.title)) {
        return true;
    }

    // Check 2: Experience requirements
    if (requiresSeniorExperience(job)) {
        return true;
    }

    // Default: Not senior (include the job)
    return false;
}

/**
 * Filter senior jobs from a batch
 * @param {Array} jobs - Array of job objects
 * @returns {Object} - { entryLevelJobs, seniorJobs, metrics }
 */
function filterSeniorJobs(jobs) {
    const entryLevelJobs = [];
    const seniorJobs = [];

    const metrics = {
        total_input: jobs.length,
        entry_level_jobs: 0,
        senior_jobs: 0,
        senior_reasons: {
            senior_title: 0,
            senior_experience: 0,
            both: 0
        }
    };

    for (const job of jobs) {
        const hasSeniorTitleFlag = hasSeniorTitle(job.title);
        const requiresSeniorExp = requiresSeniorExperience(job);

        if (hasSeniorTitleFlag || requiresSeniorExp) {
            // Track reason for filtering
            if (hasSeniorTitleFlag && requiresSeniorExp) {
                metrics.senior_reasons.both++;
            } else if (hasSeniorTitleFlag) {
                metrics.senior_reasons.senior_title++;
            } else {
                metrics.senior_reasons.senior_experience++;
            }

            metrics.senior_jobs++;
            seniorJobs.push({
                job: job,
                reason: hasSeniorTitleFlag && requiresSeniorExp ? 'both' :
                       hasSeniorTitleFlag ? 'senior_title' : 'senior_experience'
            });
        } else {
            metrics.entry_level_jobs++;
            entryLevelJobs.push(job);
        }
    }

    return {
        entryLevelJobs,
        seniorJobs,
        metrics
    };
}

/**
 * Print senior filter summary to console
 * @param {Object} metrics - Filter metrics
 */
function printSeniorFilterSummary(metrics) {
    console.log('📊 Senior Filter Summary:');
    console.log('━'.repeat(60));
    console.log(`Input jobs: ${metrics.total_input}`);
    console.log(`Entry-level jobs: ${metrics.entry_level_jobs} (${((metrics.entry_level_jobs / metrics.total_input) * 100).toFixed(1)}%)`);
    console.log(`Senior jobs filtered: ${metrics.senior_jobs} (${((metrics.senior_jobs / metrics.total_input) * 100).toFixed(1)}%)`);
    console.log('');

    if (metrics.senior_jobs > 0) {
        console.log('Senior job breakdown:');
        if (metrics.senior_reasons.senior_title > 0) {
            console.log(`  Senior title keywords: ${metrics.senior_reasons.senior_title}`);
        }
        if (metrics.senior_reasons.senior_experience > 0) {
            console.log(`  Senior experience required: ${metrics.senior_reasons.senior_experience}`);
        }
        if (metrics.senior_reasons.both > 0) {
            console.log(`  Both title + experience: ${metrics.senior_reasons.both}`);
        }
    }
}

module.exports = {
    isSeniorJob,
    hasSeniorTitle,
    requiresSeniorExperience,
    filterSeniorJobs,
    printSeniorFilterSummary,

    // Export for testing
    SENIOR_KEYWORDS,
    MIN_SENIOR_YEARS
};
