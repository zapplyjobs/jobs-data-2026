/**
 * ATS Fetcher - Wrapper for all ATS sources
 *
 * Coordinates fetching from multiple ATS platforms:
 * - Greenhouse (18 companies)
 * - Lever (4 companies)
 * - Ashby (5 companies)
 *
 * Total: 27 companies
 */

const fs = require('fs');
const path = require('path');

// Import ATS clients
const { fetchAllGreenhouseJobs } = require('./greenhouse');
const { fetchAllLeverJobs } = require('./lever');
const { fetchAllAshbyJobs } = require('./ashby');

// Load company list
const COMPANY_LIST_FILE = path.join(__dirname, 'company-list.json');
const companyList = JSON.parse(fs.readFileSync(COMPANY_LIST_FILE, 'utf8'));

/**
 * Fetch jobs from all ATS sources
 * @param {Object} options - Fetching options
 * @param {number} options.delayMs - Delay between API calls (default: 500ms)
 * @param {Array<string>} options.sources - Which sources to fetch ('greenhouse', 'lever', 'ashby'), default: all
 * @returns {Promise<Object>} - { jobs, stats }
 */
async function fetchFromAllATS(options = {}) {
    const {
        delayMs = 500,
        sources = ['greenhouse', 'lever', 'ashby']
    } = options;

    console.log('\n📦 Fetching from ATS sources...');
    console.log('━'.repeat(60));

    const results = {
        jobs: [],
        stats: {
            total_jobs: 0,
            by_source: {},
            by_company: {},
            companies_fetched: 0,
            companies_with_jobs: 0,
            fetch_duration_ms: 0
        }
    };

    const startTime = Date.now();

    try {
        // Fetch from Greenhouse
        if (sources.includes('greenhouse') && companyList.greenhouse.length > 0) {
            const greenhouseJobs = await fetchAllGreenhouseJobs(companyList.greenhouse, { delayMs });
            results.jobs.push(...greenhouseJobs);
            results.stats.by_source.greenhouse = greenhouseJobs.length;
        }

        // Fetch from Lever
        if (sources.includes('lever') && companyList.lever.length > 0) {
            const leverJobs = await fetchAllLeverJobs(companyList.lever, { delayMs });
            results.jobs.push(...leverJobs);
            results.stats.by_source.lever = leverJobs.length;
        }

        // Fetch from Ashby
        if (sources.includes('ashby') && companyList.ashby.length > 0) {
            const ashbyJobs = await fetchAllAshbyJobs(companyList.ashby, { delayMs });
            results.jobs.push(...ashbyJobs);
            results.stats.by_source.ashby = ashbyJobs.length;
        }

        // Calculate stats
        results.stats.total_jobs = results.jobs.length;
        results.stats.companies_fetched =
            (sources.includes('greenhouse') ? companyList.greenhouse.length : 0) +
            (sources.includes('lever') ? companyList.lever.length : 0) +
            (sources.includes('ashby') ? companyList.ashby.length : 0);

        // Count by company
        for (const job of results.jobs) {
            const company = job.company_name || job.company_slug;
            results.stats.by_company[company] = (results.stats.by_company[company] || 0) + 1;
        }

        results.stats.companies_with_jobs = Object.keys(results.stats.by_company).length;
        results.stats.fetch_duration_ms = Date.now() - startTime;

        // Print summary
        console.log('\n📊 ATS Fetch Summary:');
        console.log('━'.repeat(60));
        console.log(`Total jobs: ${results.stats.total_jobs}`);
        console.log(`Companies fetched: ${results.stats.companies_fetched}`);
        console.log(`Companies with jobs: ${results.stats.companies_with_jobs}`);
        console.log(`Duration: ${(results.stats.fetch_duration_ms / 1000).toFixed(1)}s`);
        console.log('');
        console.log('By source:');
        for (const [source, count] of Object.entries(results.stats.by_source)) {
            console.log(`  ${source}: ${count} jobs`);
        }

        return results;

    } catch (error) {
        console.error('\n❌ Error fetching from ATS sources:', error.message);
        throw error;
    }
}

/**
 * Get usage stats (for compatibility with JSearch fetcher)
 * @returns {Object} Usage statistics
 */
function getUsageStats() {
    return {
        companies_total: companyList.greenhouse.length + companyList.lever.length + companyList.ashby.length,
        greenhouse_companies: companyList.greenhouse.length,
        lever_companies: companyList.lever.length,
        ashby_companies: companyList.ashby.length
    };
}

module.exports = {
    fetchFromAllATS,
    getUsageStats
};
