/**
 * Job Validator - Malformed Field Handling
 *
 * Validates and normalizes jobs before processing.
 * Based on Task #2 research (2026-02-15)
 *
 * Strategy: SKIP jobs with missing required fields, AUTO-FIX optional fields
 *
 * Required fields (Tier 1): title, company_name, url
 * Auto-fix fields (Tier 2): job_city, job_state (parse from location)
 * Optional fields (Tier 3): experience_level, employment_type
 */

/**
 * Validate job has all required fields
 * @param {Object} job - Job object to validate
 * @returns {boolean} - True if valid, false if should be skipped
 */
function isValidJob(job) {
    // Tier 1: REQUIRED - title
    if (!job.title || typeof job.title !== 'string' || job.title.trim().length < 5) {
        return false;
    }

    // Tier 1: REQUIRED - company_name
    if (!job.company_name || typeof job.company_name !== 'string' || job.company_name.trim().length < 2) {
        return false;
    }

    // Tier 1: REQUIRED - url
    if (!job.url || typeof job.url !== 'string' || !job.url.startsWith('http')) {
        return false;
    }

    return true;
}

/**
 * Normalize job fields (auto-fix missing data)
 * @param {Object} job - Job object to normalize
 * @returns {Object} - Normalized job object
 */
function normalizeJob(job) {
    // Tier 2: AUTO-FIX - Extract state from location if missing
    // Pattern: "San Francisco, CA" → "CA"
    if ((!job.job_state || job.job_state === '') && job.location) {
        const stateMatch = job.location.match(/,\s*([A-Z]{2})\b/);
        if (stateMatch) {
            job.job_state = stateMatch[1];
        }
    }

    // Tier 2: AUTO-FIX - Extract city from location if missing
    // Pattern: "San Francisco, CA" → "San Francisco"
    if ((!job.job_city || job.job_city === '') && job.location) {
        const cityMatch = job.location.match(/^([^,]+)/);
        if (cityMatch) {
            job.job_city = cityMatch[1].trim();
        }
    }

    // Tier 3: OPTIONAL - Don't modify experience_level or employment_type
    // These fields are optional and not critical for job display

    return job;
}

/**
 * Validate and normalize a batch of jobs
 * @param {Array} jobs - Array of job objects
 * @returns {Object} - { validJobs, invalidJobs, metrics }
 */
function validateAndNormalizeJobs(jobs) {
    const validJobs = [];
    const invalidJobs = [];

    const metrics = {
        total_input: jobs.length,
        valid_jobs: 0,
        invalid_jobs: 0,
        invalid_reasons: {
            missing_title: 0,
            missing_company: 0,
            missing_url: 0,
            invalid_title_length: 0,
            invalid_company_length: 0,
            invalid_url_format: 0
        },
        normalized_fields: {
            state_extracted: 0,
            city_extracted: 0
        }
    };

    for (const job of jobs) {
        // Track why job is invalid (for debugging)
        let invalidReason = null;

        // Validate title
        if (!job.title || typeof job.title !== 'string') {
            invalidReason = 'missing_title';
        } else if (job.title.trim().length < 5) {
            invalidReason = 'invalid_title_length';
        }
        // Validate company
        else if (!job.company_name || typeof job.company_name !== 'string') {
            invalidReason = 'missing_company';
        } else if (job.company_name.trim().length < 2) {
            invalidReason = 'invalid_company_length';
        }
        // Validate URL
        else if (!job.url || typeof job.url !== 'string') {
            invalidReason = 'missing_url';
        } else if (!job.url.startsWith('http')) {
            invalidReason = 'invalid_url_format';
        }

        // Process based on validation result
        if (invalidReason) {
            metrics.invalid_reasons[invalidReason]++;
            metrics.invalid_jobs++;
            invalidJobs.push({
                job: job,
                reason: invalidReason
            });
        } else {
            // Track state before normalization
            const hadState = job.job_state && job.job_state !== '';
            const hadCity = job.job_city && job.job_city !== '';

            // Normalize the job
            const normalizedJob = normalizeJob(job);

            // Track what was normalized
            if (!hadState && normalizedJob.job_state) {
                metrics.normalized_fields.state_extracted++;
            }
            if (!hadCity && normalizedJob.job_city) {
                metrics.normalized_fields.city_extracted++;
            }

            metrics.valid_jobs++;
            validJobs.push(normalizedJob);
        }
    }

    return {
        validJobs,
        invalidJobs,
        metrics
    };
}

/**
 * Print validation summary to console
 * @param {Object} metrics - Validation metrics
 */
function printValidationSummary(metrics) {
    console.log('📊 Validation Summary:');
    console.log('━'.repeat(60));
    console.log(`Input jobs: ${metrics.total_input}`);
    console.log(`Valid jobs: ${metrics.valid_jobs} (${((metrics.valid_jobs / metrics.total_input) * 100).toFixed(1)}%)`);
    console.log(`Invalid jobs: ${metrics.invalid_jobs} (${((metrics.invalid_jobs / metrics.total_input) * 100).toFixed(1)}%)`);
    console.log('');

    if (metrics.invalid_jobs > 0) {
        console.log('Invalid job breakdown:');
        for (const [reason, count] of Object.entries(metrics.invalid_reasons)) {
            if (count > 0) {
                console.log(`  ${reason}: ${count}`);
            }
        }
        console.log('');
    }

    const totalNormalized = metrics.normalized_fields.state_extracted + metrics.normalized_fields.city_extracted;
    if (totalNormalized > 0) {
        console.log('Fields auto-fixed:');
        if (metrics.normalized_fields.state_extracted > 0) {
            console.log(`  State extracted from location: ${metrics.normalized_fields.state_extracted}`);
        }
        if (metrics.normalized_fields.city_extracted > 0) {
            console.log(`  City extracted from location: ${metrics.normalized_fields.city_extracted}`);
        }
    }
}

module.exports = {
    isValidJob,
    normalizeJob,
    validateAndNormalizeJobs,
    printValidationSummary
};
