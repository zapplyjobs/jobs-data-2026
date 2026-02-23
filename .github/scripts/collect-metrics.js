#!/usr/bin/env node

/**
 * Collect Metrics for Aggregator Monitoring
 *
 * Gathers metrics from all job board repos for visibility
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

// Configuration
const METRICS_DIR = path.join(process.cwd(), '.github', 'data', 'metrics');
const LATEST_FILE = path.join(METRICS_DIR, 'latest.json');
const HISTORY_FILE = path.join(METRICS_DIR, 'history.jsonl');

// Repositories to monitor
const REPOS = [
  { owner: 'zapplyjobs', repo: 'New-Grad-Jobs-2026', type: 'main', name: 'New-Grad' },
  { owner: 'zapplyjobs', repo: 'Internships-2026', type: 'main', name: 'Internships' },
  { owner: 'zapplyjobs', repo: 'jobs-data-2026', type: 'aggregator', name: 'Aggregator' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Software-Engineering-Jobs-2026', type: 'seo', name: 'Software' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Data-Science-Jobs-2026', type: 'seo', name: 'Data-Science' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Hardware-Engineering-Jobs-2026', type: 'seo', name: 'Hardware' },
  { owner: 'zapplyjobs', repo: 'New-Grad-Nursing-Jobs-2026', type: 'seo', name: 'Nursing' }
];

/**
 * Fetch file content from GitHub API
 */
function fetchGitHubFile(owner, repo, filePath) {
  return new Promise((resolve, reject) => {
    const url = `https://raw.githubusercontent.com/${owner}/${repo}/main/${filePath}`;

    https.get(url, {
      headers: { 'User-Agent': 'Zapply-Metrics-Bot' }
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        if (res.statusCode === 200) {
          resolve(data);
        } else {
          resolve(null);
        }
      });
    }).on('error', reject);
  });
}

/**
 * Fetch workflow run status
 */
function fetchWorkflowStatus(owner, repo, workflowName) {
  return new Promise((resolve, reject) => {
    const url = `https://api.github.com/repos/${owner}/${repo}/actions/runs?per_page=1`;

    https.get(url, {
      headers: {
        'User-Agent': 'Zapply-Metrics-Bot',
        'Authorization': `Bearer ${process.env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github.v3+json'
      }
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          const json = JSON.parse(data);
          if (json.workflow_runs && json.workflow_runs.length > 0) {
            const run = json.workflow_runs[0];
            resolve({
              id: run.id,
              status: run.status,
              conclusion: run.conclusion,
              createdAt: run.created_at,
              updatedAt: run.updated_at
            });
          } else {
            resolve(null);
          }
        } catch (error) {
          reject(error);
        }
      });
    }).on('error', reject);
  });
}

/**
 * Get metrics for a single repo
 */
async function getRepoMetrics(repo) {
  const metrics = {
    name: repo.name,
    type: repo.type,
    jobs: 0,
    lastUpdate: null,
    workflowStatus: null,
    error: null
  };

  try {
    // Try to fetch current_jobs.json (main repos) or current_jobs.json (SEO repos)
    let jobsData = null;
    try {
      jobsData = await fetchGitHubFile(repo.owner, repo.repo, '.github/data/current_jobs.json');
    } catch {
      // Try new_jobs.json as fallback
      try {
        jobsData = await fetchGitHubFile(repo.owner, repo.repo, '.github/data/new_jobs.json');
      } catch {
        // Try root level current_jobs.json (SEO repos)
        try {
          jobsData = await fetchGitHubFile(repo.owner, repo.repo, 'current_jobs.json');
        } catch {
          // No jobs data
        }
      }
    }

    if (jobsData) {
      const jobs = JSON.parse(jobsData);
      metrics.jobs = Array.isArray(jobs) ? jobs.length : 0;
    }

    // Get workflow status
    try {
      const workflow = await fetchWorkflowStatus(repo.owner, repo.repo);
      if (workflow) {
        metrics.workflowStatus = {
          id: workflow.id,
          status: workflow.status,
          conclusion: workflow.conclusion,
          lastRun: workflow.updatedAt
        };
      }
    } catch {
      // Workflow status unavailable
    }

  } catch (error) {
    metrics.error = error.message;
  }

  return metrics;
}

/**
 * Main execution
 */
async function main() {
  console.log('🔍 Collecting Aggregator Metrics...');

  // Ensure metrics directory exists
  if (!fs.existsSync(METRICS_DIR)) {
    fs.mkdirSync(METRICS_DIR, { recursive: true });
  }

  const timestamp = new Date().toISOString();
  const metrics = {
    timestamp,
    repos: {},
    summary: {
      totalJobs: 0,
      operationalRepos: 0,
      failedRepos: 0
    }
  };

  // Collect metrics from all repos
  for (const repo of REPOS) {
    console.log(`  📊 ${repo.name}...`);
    const repoMetrics = await getRepoMetrics(repo);
    metrics.repos[repo.name] = repoMetrics;

    // Update summary
    metrics.summary.totalJobs += repoMetrics.jobs;
    if (repoMetrics.workflowStatus?.conclusion === 'success') {
      metrics.summary.operationalRepos++;
    }
    if (repoMetrics.workflowStatus?.conclusion === 'failure') {
      metrics.summary.failedRepos++;
    }
  }

  // Print summary
  console.log('');
  console.log('📈 Metrics Summary:');
  console.log(`  Total Jobs: ${metrics.summary.totalJobs}`);
  console.log(`  Operational: ${metrics.summary.operationalRepos}`);
  console.log(`  Failed: ${metrics.summary.failedRepos}`);
  console.log('');

  // Write latest metrics
  fs.writeFileSync(LATEST_FILE, JSON.stringify(metrics, null, 2), 'utf8');
  console.log(`✅ Wrote metrics to: ${LATEST_FILE}`);

  // Append to history
  const historyEntry = JSON.stringify(metrics);
  fs.appendFileSync(HISTORY_FILE, historyEntry + '\n', 'utf8');
  console.log(`✅ Appended to history: ${HISTORY_FILE}`);

  // Print repo breakdown
  console.log('');
  console.log('📊 Repo Breakdown:');
  for (const [name, repo] of Object.entries(metrics.repos)) {
    const status = repo.workflowStatus?.conclusion || 'unknown';
    const statusEmoji = status === 'success' ? '✅' : status === 'failure' ? '❌' : '⚠️';
    console.log(`  ${statusEmoji} ${name}: ${repo.jobs} jobs (${status})`);
  }

  console.log('');
  console.log('✅ Metrics collection complete');
}

main().catch(error => {
  console.error('❌ Error:', error.message);
  process.exit(1);
});
